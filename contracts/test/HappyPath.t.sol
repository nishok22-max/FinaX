// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ForkBase} from "./ForkBase.t.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {LiquidationShieldVault} from "../src/LiquidationShieldVault.sol";

/// @title Phase 1 — Happy path
/// @notice End-to-end atomic rescue on the fork: flash-loan -> repay -> transferFrom+withdraw
/// collateral -> swap -> repay flash -> multi-invariant HealthGuard. Verifies HF improves, debt
/// falls, and residual collateral is returned to the borrower.
contract HappyPathTest is ForkBase {
    LiquidationShieldVault internal vault;
    address internal keeper;

    function setUp() public override {
        super.setUp();
        keeper = makeAddr("keeper");
        vault = new LiquidationShieldVault(POOL_ADDRESSES_PROVIDER, SWAP_ROUTER, keeper);

        // Open a moderately levered position (~70% of borrow capacity).
        _openPosition(2 ether, 0);
        (,, uint256 availableBase,,,) = pool.getUserAccountData(borrower);
        uint256 usdcAmount = (availableBase * 1e6 * 70) / (oracle.getAssetPrice(USDC) * 100);
        vm.prank(borrower);
        pool.borrow(USDC, usdcAmount, VARIABLE_RATE, 0, borrower);

        // Borrower opt-in: grant the vault an aWETH allowance.
        address aWeth = pool.getReserveData(WETH).aTokenAddress;
        vm.prank(borrower);
        IERC20(aWeth).approve(address(vault), type(uint256).max);
    }

    function _usdcDebt() internal view returns (uint256) {
        return IERC20(pool.getReserveData(USDC).variableDebtTokenAddress).balanceOf(borrower);
    }

    function _signedParams() internal view returns (LiquidationShieldVault.RiskParams memory p, bytes memory sig) {
        address[] memory allowed = new address[](1);
        allowed[0] = WETH;
        p = LiquidationShieldVault.RiskParams({
            borrower: borrower,
            hfTriggerBps: 11500,
            hfTargetBaseBps: 10500,
            volCoeffK: 0,
            hfTargetMaxBps: 30000,
            maxSlippageBps: 200,
            maxCostBps: 300,
            allowedCollaterals: allowed,
            nonce: 1,
            deadline: block.timestamp + 1 hours
        });
        bytes32 digest = vault.hashRiskParams(p);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(borrowerPk, digest);
        sig = abi.encodePacked(r, s, v);
    }

    function test_atomicRescueImprovesHealth() public {
        uint256 hfBefore = _healthFactor(borrower);
        uint256 debtBefore = _usdcDebt();
        uint256 wethWalletBefore = IERC20(WETH).balanceOf(borrower);

        uint256 repayAmount = (debtBefore * 30) / 100; // repay 30% of the USDC debt
        uint256 outNeeded = repayAmount + (repayAmount * 5) / 10000; // + 0.05% flash premium
        uint256 amountInMaximum = (_quoteWethIn(outNeeded) * 101) / 100; // 1% buffer over quote

        // Runtime target = current HF (contract must guarantee HF does not worsen); within the band.
        uint256 hfTargetBps = hfBefore / 1e14;

        (LiquidationShieldVault.RiskParams memory p, bytes memory sig) = _signedParams();

        vm.prank(keeper);
        vault.executeProtection(p, sig, USDC, repayAmount, WETH, amountInMaximum, FEE, hfTargetBps);

        uint256 hfAfter = _healthFactor(borrower);
        assertGe(hfAfter, hfBefore, "HF must not worsen");
        assertLt(_usdcDebt(), debtBefore, "debt must be reduced");
        assertGt(IERC20(WETH).balanceOf(borrower), wethWalletBefore, "residual collateral returned to borrower");
        // Vault holds no residual funds.
        assertEq(IERC20(USDC).balanceOf(address(vault)), 0, "no USDC left in vault");
        assertEq(IERC20(WETH).balanceOf(address(vault)), 0, "no WETH left in vault");
    }
}
