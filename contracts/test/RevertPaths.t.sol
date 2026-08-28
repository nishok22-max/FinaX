// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ForkBase} from "./ForkBase.t.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {LiquidationShieldVault} from "../src/LiquidationShieldVault.sol";

/// @title Phase 2 — Revert safety / no-worse guarantee
/// @notice Every failure mode must revert the whole transaction, leaving the position unchanged.
contract RevertPathsTest is ForkBase {
    LiquidationShieldVault internal vault;
    address internal keeper;
    address internal aWeth;

    function setUp() public override {
        super.setUp();
        keeper = makeAddr("keeper");
        vault = new LiquidationShieldVault(POOL_ADDRESSES_PROVIDER, SWAP_ROUTER, keeper);

        _openPosition(2 ether, 0);
        (,, uint256 availableBase,,,) = pool.getUserAccountData(borrower);
        uint256 usdcAmount = (availableBase * 1e6 * 70) / (oracle.getAssetPrice(USDC) * 100);
        vm.prank(borrower);
        pool.borrow(USDC, usdcAmount, VARIABLE_RATE, 0, borrower);

        aWeth = pool.getReserveData(WETH).aTokenAddress;
    }

    function _optIn() internal {
        vm.prank(borrower);
        IERC20(aWeth).approve(address(vault), type(uint256).max);
    }

    function _usdcDebt() internal view returns (uint256) {
        return IERC20(pool.getReserveData(USDC).variableDebtTokenAddress).balanceOf(borrower);
    }

    function _params(uint256 nonce, uint256 deadline, uint256 maxTargetBps)
        internal
        view
        returns (LiquidationShieldVault.RiskParams memory p)
    {
        address[] memory allowed = new address[](1);
        allowed[0] = WETH;
        p = LiquidationShieldVault.RiskParams({
            borrower: borrower,
            hfTriggerBps: 11500,
            hfTargetBaseBps: 10500,
            volCoeffK: 0,
            hfTargetMaxBps: maxTargetBps,
            maxSlippageBps: 200,
            maxCostBps: 300,
            allowedCollaterals: allowed,
            nonce: nonce,
            deadline: deadline
        });
    }

    function _sign(LiquidationShieldVault.RiskParams memory p, uint256 pk) internal view returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(pk, vault.hashRiskParams(p));
        return abi.encodePacked(r, s, v);
    }

    function test_revertBadSignature() public {
        _optIn();
        (, uint256 attackerPk) = makeAddrAndKey("attacker");
        LiquidationShieldVault.RiskParams memory p = _params(1, block.timestamp + 1 hours, 30000);
        bytes memory badSig = _sign(p, attackerPk); // signed by the wrong key

        vm.prank(keeper);
        vm.expectRevert(LiquidationShieldVault.BadSignature.selector);
        vault.executeProtection(p, badSig, USDC, 100e6, WETH, 1 ether, FEE, 11000);
    }

    function test_revertExpiredDeadline() public {
        _optIn();
        LiquidationShieldVault.RiskParams memory p = _params(1, block.timestamp - 1, 30000);
        bytes memory sig = _sign(p, borrowerPk);

        vm.prank(keeper);
        vm.expectRevert(LiquidationShieldVault.Expired.selector);
        vault.executeProtection(p, sig, USDC, 100e6, WETH, 1 ether, FEE, 11000);
    }

    function test_revertUnauthorizedCaller() public {
        _optIn();
        LiquidationShieldVault.RiskParams memory p = _params(1, block.timestamp + 1 hours, 30000);
        bytes memory sig = _sign(p, borrowerPk);

        vm.prank(makeAddr("random"));
        vm.expectRevert(LiquidationShieldVault.NotAuthorized.selector);
        vault.executeProtection(p, sig, USDC, 100e6, WETH, 1 ether, FEE, 11000);
    }

    function test_revertTargetOutOfBand() public {
        _optIn();
        LiquidationShieldVault.RiskParams memory p = _params(1, block.timestamp + 1 hours, 12000);
        bytes memory sig = _sign(p, borrowerPk);

        // Runtime target above the signed ceiling (12000) => revert before any external call.
        vm.prank(keeper);
        vm.expectRevert(LiquidationShieldVault.TargetOutOfBand.selector);
        vault.executeProtection(p, sig, USDC, 100e6, WETH, 1 ether, FEE, 20000);
    }

    function test_revertNoATokenAllowance() public {
        // Deliberately DO NOT opt in.
        LiquidationShieldVault.RiskParams memory p = _params(1, block.timestamp + 1 hours, 30000);
        bytes memory sig = _sign(p, borrowerPk);
        uint256 repayAmount = (_usdcDebt() * 20) / 100;
        uint256 amountInMax = (_quoteWethIn(repayAmount) * 2);

        vm.prank(keeper);
        vm.expectRevert(); // aToken transferFrom reverts (no allowance)
        vault.executeProtection(p, sig, USDC, repayAmount, WETH, amountInMax, FEE, 11000);
    }

    function test_revertExcessSlippage() public {
        _optIn();
        LiquidationShieldVault.RiskParams memory p = _params(1, block.timestamp + 1 hours, 30000);
        bytes memory sig = _sign(p, borrowerPk);
        uint256 repayAmount = (_usdcDebt() * 20) / 100;
        uint256 tooLow = _quoteWethIn(repayAmount) / 2; // amountInMaximum below the real cost

        vm.prank(keeper);
        vm.expectRevert(); // Uniswap exactOutputSingle reverts (STF / max input exceeded)
        vault.executeProtection(p, sig, USDC, repayAmount, WETH, tooLow, FEE, 11000);
    }

    function test_revertHealthBelowTarget() public {
        _optIn();
        LiquidationShieldVault.RiskParams memory p = _params(1, block.timestamp + 1 hours, 30000);
        bytes memory sig = _sign(p, borrowerPk);

        // Tiny repay cannot reach an unrealistically high runtime target (2.9) => guard reverts.
        uint256 repayAmount = (_usdcDebt() * 5) / 100;
        uint256 amountInMax = (_quoteWethIn(repayAmount + (repayAmount * 5) / 10000) * 101) / 100;

        vm.prank(keeper);
        vm.expectRevert(LiquidationShieldVault.HealthBelowTarget.selector);
        vault.executeProtection(p, sig, USDC, repayAmount, WETH, amountInMax, FEE, 29000);
    }

    function test_revertNonceReuse() public {
        _optIn();
        LiquidationShieldVault.RiskParams memory p = _params(7, block.timestamp + 1 hours, 30000);
        bytes memory sig = _sign(p, borrowerPk);

        uint256 hfNow = _healthFactor(borrower) / 1e14;
        uint256 repayAmount = (_usdcDebt() * 30) / 100;
        uint256 amountInMax = (_quoteWethIn(repayAmount + (repayAmount * 5) / 10000) * 101) / 100;

        vm.prank(keeper);
        vault.executeProtection(p, sig, USDC, repayAmount, WETH, amountInMax, FEE, hfNow);

        // Re-using the same nonce must revert.
        vm.prank(keeper);
        vm.expectRevert(LiquidationShieldVault.NonceUsed.selector);
        vault.executeProtection(p, sig, USDC, repayAmount, WETH, amountInMax, FEE, hfNow);
    }
}
