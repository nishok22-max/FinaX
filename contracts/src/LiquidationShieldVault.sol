// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

import {IPool} from "./interfaces/IPool.sol";
import {IPoolAddressesProvider} from "./interfaces/IPoolAddressesProvider.sol";
import {IPriceOracleGetter} from "./interfaces/IPriceOracleGetter.sol";
import {IFlashLoanSimpleReceiver} from "./interfaces/IFlashLoanSimpleReceiver.sol";
import {ISwapRouter02} from "./interfaces/ISwapRouter02.sol";
import {HealthMath} from "./libraries/HealthMath.sol";

/// @title LiquidationShieldVault
/// @notice Autonomous, atomic liquidation-protection vault for Aave V3 positions on Arbitrum One.
/// @dev Single contract with logical internal modules. The full restructuring sequence runs inside
/// the Aave flash-loan callback and reverts unless every no-worse invariant holds (§4.3 of the PRD).
///
/// Permission model (validated in Sprint 0 / PoC_AavePermissions.t.sol):
///  - Repay of the borrower's debt is PERMISSIONLESS (`Pool.repay(..., onBehalfOf=borrower)`).
///  - Collateral moves ONLY via the borrower's aToken allowance/permit:
///    `aToken.transferFrom(borrower, vault, amt)` then `Pool.withdraw(collateral, .., vault)`.
///  - Credit delegation is NOT used. Repay MUST precede withdrawal so Aave's `finalizeTransfer`
///    health check on the aToken transfer passes.
contract LiquidationShieldVault is IFlashLoanSimpleReceiver, ReentrancyGuard, EIP712, Ownable {
    using SafeERC20 for IERC20;

    // --- Types -------------------------------------------------------------

    /// @notice Borrower-signed risk policy (EIP-712). The keeper acts only within these bounds.
    struct RiskParams {
        address borrower;
        uint256 hfTriggerBps; // when the keeper should act (off-chain use)
        uint256 hfTargetBaseBps; // floor of the runtime target band
        uint256 volCoeffK; // dynamic-buffer coefficient (off-chain use)
        uint256 hfTargetMaxBps; // ceiling of the runtime target band
        uint16 maxSlippageBps; // swap slippage bound (off-chain -> amountInMaximum)
        uint16 maxCostBps; // on-chain economic bound (collateral spent vs debt value)
        address[] allowedCollaterals;
        uint256 nonce;
        uint256 deadline;
    }

    /// @dev Encoded into the flash-loan `params` and decoded inside `executeOperation`.
    struct FlashParams {
        address borrower;
        address collateralAsset;
        uint256 amountInMaximum;
        uint24 uniFeeTier;
        uint256 hfTargetWad;
        uint16 maxCostBps;
        uint256 debtBefore;
        uint256 collatBefore;
    }

    // --- Constants / immutables -------------------------------------------

    uint256 internal constant VARIABLE_RATE = 2; // Aave V3 variable interest-rate mode
    uint256 internal constant BPS = 1e4;

    bytes32 private constant RISK_PARAMS_TYPEHASH = keccak256(
        "RiskParams(address borrower,uint256 hfTriggerBps,uint256 hfTargetBaseBps,uint256 volCoeffK,uint256 hfTargetMaxBps,uint16 maxSlippageBps,uint16 maxCostBps,address[] allowedCollaterals,uint256 nonce,uint256 deadline)"
    );

    IPoolAddressesProvider public immutable ADDRESSES_PROVIDER;
    IPool public immutable POOL;
    ISwapRouter02 public immutable SWAP_ROUTER;

    // --- Storage -----------------------------------------------------------

    address public keeper;
    mapping(address => mapping(uint256 => bool)) public usedNonces;

    // --- Events ------------------------------------------------------------

    event ProtectionExecuted(
        address indexed borrower,
        address debtAsset,
        uint256 repayAmount,
        address collateralAsset,
        uint256 hfBefore,
        uint256 hfAfter
    );
    event KeeperUpdated(address indexed keeper);

    // --- Errors ------------------------------------------------------------

    error NotAuthorized();
    error BadSignature();
    error NonceUsed();
    error Expired();
    error CollateralNotAllowed();
    error NoDebt();
    error TargetOutOfBand();
    error CallerNotPool();
    error BadInitiator();
    error AssetMismatch();
    error CostExceeded();
    error HealthBelowTarget();
    error DebtNotReduced();
    error LeverageIncreased();

    // --- Constructor -------------------------------------------------------

    constructor(address addressesProvider, address swapRouter, address keeper_)
        EIP712("LiquidationShieldVault", "1")
        Ownable(msg.sender)
    {
        ADDRESSES_PROVIDER = IPoolAddressesProvider(addressesProvider);
        POOL = IPool(IPoolAddressesProvider(addressesProvider).getPool());
        SWAP_ROUTER = ISwapRouter02(swapRouter);
        keeper = keeper_;
        emit KeeperUpdated(keeper_);
    }

    // --- Admin -------------------------------------------------------------

    function setKeeper(address keeper_) external onlyOwner {
        keeper = keeper_;
        emit KeeperUpdated(keeper_);
    }

    // --- Entry point -------------------------------------------------------

    /// @notice Atomically restructure `borrower`'s Aave position back above a runtime HF target.
    /// @param hfTargetBps Runtime target chosen by the keeper; must sit within the borrower's
    ///        signed band [hfTargetBaseBps, hfTargetMaxBps]. (Refinement over the doc's illustrative
    ///        signature: the dynamic, volatility-derived target is computed off-chain and passed here,
    ///        while the contract enforces both the band and the achieved HF.)
    function executeProtection(
        RiskParams calldata p,
        bytes calldata sig,
        address debtAsset,
        uint256 repayAmount,
        address collateralAsset,
        uint256 amountInMaximum,
        uint24 uniFeeTier,
        uint256 hfTargetBps
    ) external nonReentrant {
        _validateParams(p, sig, collateralAsset);
        if (hfTargetBps < p.hfTargetBaseBps || hfTargetBps > p.hfTargetMaxBps) revert TargetOutOfBand();

        (uint256 collatBefore, uint256 debtBefore,,,, uint256 hfBefore) = POOL.getUserAccountData(p.borrower);
        if (debtBefore == 0) revert NoDebt();

        FlashParams memory fp = FlashParams({
            borrower: p.borrower,
            collateralAsset: collateralAsset,
            amountInMaximum: amountInMaximum,
            uniFeeTier: uniFeeTier,
            hfTargetWad: HealthMath.bpsToWad(hfTargetBps),
            maxCostBps: p.maxCostBps,
            debtBefore: debtBefore,
            collatBefore: collatBefore
        });

        POOL.flashLoanSimple(address(this), debtAsset, repayAmount, abi.encode(fp, debtAsset), 0);

        (,,,,, uint256 hfAfter) = POOL.getUserAccountData(p.borrower);
        emit ProtectionExecuted(p.borrower, debtAsset, repayAmount, collateralAsset, hfBefore, hfAfter);
    }

    // --- Flash-loan callback (the atomic body) -----------------------------

    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external override returns (bool) {
        if (msg.sender != address(POOL)) revert CallerNotPool();
        if (initiator != address(this)) revert BadInitiator();

        (FlashParams memory fp, address debtAsset) = abi.decode(params, (FlashParams, address));
        if (asset != debtAsset) revert AssetMismatch();

        // 1. Repay borrower debt (permissionless) — improves HF FIRST so the aToken transfer passes.
        _repayDebt(debtAsset, amount, fp.borrower);

        // 2. Pull borrower collateral via aToken allowance, then withdraw underlying to the vault.
        _withdrawCollateral(fp.collateralAsset, fp.borrower, fp.amountInMaximum);

        // 3. Swap collateral -> exactly (amount + premium) of debt asset.
        uint256 outNeeded = amount + premium;
        uint256 amountIn = _executeSwap(fp.collateralAsset, debtAsset, fp.uniFeeTier, outNeeded, fp.amountInMaximum);

        // 4. Return residual collateral to borrower.
        uint256 leftover = IERC20(fp.collateralAsset).balanceOf(address(this));
        if (leftover > 0) IERC20(fp.collateralAsset).safeTransfer(fp.borrower, leftover);

        // 5. Economic bound: collateral spent must not exceed debt value by more than maxCostBps.
        _enforceCost(fp.collateralAsset, debtAsset, amountIn, outNeeded, fp.maxCostBps);

        // 6. Multi-invariant no-worse HealthGuard.
        _verifyHealthFactor(fp);

        // 7. Approve the pool to reclaim flash amount + premium.
        IERC20(debtAsset).forceApprove(address(POOL), outNeeded);
        return true;
    }

    // --- Internal modules --------------------------------------------------

    function _validateParams(RiskParams calldata p, bytes calldata sig, address collateralAsset) internal {
        if (msg.sender != keeper && msg.sender != p.borrower) revert NotAuthorized();
        if (block.timestamp > p.deadline) revert Expired();
        if (usedNonces[p.borrower][p.nonce]) revert NonceUsed();
        if (!_isAllowed(p.allowedCollaterals, collateralAsset)) revert CollateralNotAllowed();

        bytes32 digest = _hashTypedDataV4(_structHash(p));
        address signer = ECDSA.recover(digest, sig);
        if (signer != p.borrower) revert BadSignature();

        usedNonces[p.borrower][p.nonce] = true;
    }

    function _repayDebt(address debtAsset, uint256 amount, address borrower) internal {
        IERC20(debtAsset).forceApprove(address(POOL), amount);
        POOL.repay(debtAsset, amount, VARIABLE_RATE, borrower);
    }

    function _withdrawCollateral(address collateralAsset, address borrower, uint256 amount) internal {
        address aToken = POOL.getReserveData(collateralAsset).aTokenAddress;
        // Requires the borrower's aToken allowance/permit (opt-in). Reverts here if not granted.
        IERC20(aToken).safeTransferFrom(borrower, address(this), amount);
        // Burn the vault's now-held aTokens for the underlying collateral.
        POOL.withdraw(collateralAsset, type(uint256).max, address(this));
    }

    function _executeSwap(
        address collateralAsset,
        address debtAsset,
        uint24 feeTier,
        uint256 amountOut,
        uint256 amountInMaximum
    ) internal returns (uint256 amountIn) {
        IERC20(collateralAsset).forceApprove(address(SWAP_ROUTER), amountInMaximum);
        amountIn = SWAP_ROUTER.exactOutputSingle(
            ISwapRouter02.ExactOutputSingleParams({
                tokenIn: collateralAsset,
                tokenOut: debtAsset,
                fee: feeTier,
                recipient: address(this),
                amountOut: amountOut,
                amountInMaximum: amountInMaximum,
                sqrtPriceLimitX96: 0
            })
        );
        IERC20(collateralAsset).forceApprove(address(SWAP_ROUTER), 0);
    }

    function _enforceCost(address collat, address debt, uint256 amountIn, uint256 outNeeded, uint16 maxCostBps)
        internal
        view
    {
        IPriceOracleGetter oracle = IPriceOracleGetter(ADDRESSES_PROVIDER.getPriceOracle());
        uint256 valIn = (amountIn * oracle.getAssetPrice(collat)) / (10 ** IERC20Metadata(collat).decimals());
        uint256 valOut = (outNeeded * oracle.getAssetPrice(debt)) / (10 ** IERC20Metadata(debt).decimals());
        if (valIn > (valOut * (BPS + maxCostBps)) / BPS) revert CostExceeded();
    }

    function _verifyHealthFactor(FlashParams memory fp) internal view {
        (uint256 collatAfter, uint256 debtAfter,,,, uint256 hfAfter) = POOL.getUserAccountData(fp.borrower);
        if (hfAfter < fp.hfTargetWad) revert HealthBelowTarget();
        if (debtAfter >= fp.debtBefore) revert DebtNotReduced();
        if (HealthMath.leverageWad(debtAfter, collatAfter) > HealthMath.leverageWad(fp.debtBefore, fp.collatBefore)) {
            revert LeverageIncreased();
        }
    }

    // --- Views / helpers ---------------------------------------------------

    /// @notice EIP-712 digest for a RiskParams payload — used by clients/tests to sign.
    function hashRiskParams(RiskParams calldata p) external view returns (bytes32) {
        return _hashTypedDataV4(_structHash(p));
    }

    function domainSeparator() external view returns (bytes32) {
        return _domainSeparatorV4();
    }

    function _structHash(RiskParams calldata p) internal pure returns (bytes32) {
        return keccak256(
            abi.encode(
                RISK_PARAMS_TYPEHASH,
                p.borrower,
                p.hfTriggerBps,
                p.hfTargetBaseBps,
                p.volCoeffK,
                p.hfTargetMaxBps,
                p.maxSlippageBps,
                p.maxCostBps,
                keccak256(abi.encodePacked(p.allowedCollaterals)),
                p.nonce,
                p.deadline
            )
        );
    }

    function _isAllowed(address[] calldata list, address asset) internal pure returns (bool) {
        for (uint256 i = 0; i < list.length; i++) {
            if (list[i] == asset) return true;
        }
        return false;
    }
}
