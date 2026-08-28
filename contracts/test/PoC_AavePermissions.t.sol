// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ForkBase} from "./ForkBase.t.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

/// @title Sprint 0 — Gate A: Aave V3 permission proof-of-concept
/// @notice Proves the exact authorization the vault relies on, BEFORE building the production vault:
///   1. Repaying a borrower's debt is PERMISSIONLESS.
///   2. Moving a borrower's collateral requires an aToken allowance (transferFrom -> withdraw);
///      `Pool.withdraw` has no `onBehalfOf`. Credit delegation is NOT the mechanism.
///   3. Repay must precede withdrawal — Aave's `finalizeTransfer`/withdraw health check blocks
///      collateral leaving while it would push HF below 1.
contract PoC_AavePermissionsTest is ForkBase {
    function _openTightPosition() internal {
        _openPosition(2 ether, 0); // supply 2 WETH, no debt yet
        (,, uint256 availableBase,,,) = pool.getUserAccountData(borrower);
        uint256 usdcPrice = oracle.getAssetPrice(USDC);
        // Borrow ~90% of the borrowable capacity so HF is close to (but above) the boundary.
        uint256 usdcAmount = (availableBase * 1e6 * 90) / (usdcPrice * 100);
        vm.prank(borrower);
        pool.borrow(USDC, usdcAmount, VARIABLE_RATE, 0, borrower);
    }

    function test_repayIsPermissionless() public {
        _openTightPosition();
        (, uint256 debtBefore,,,,) = pool.getUserAccountData(borrower);
        assertGt(debtBefore, 0, "expected debt");

        address repayer = makeAddr("repayer");
        uint256 repayUsdc = 200e6;
        deal(USDC, repayer, repayUsdc); // if this fails on your fork, use USDC.e

        vm.startPrank(repayer);
        IERC20(USDC).approve(address(pool), repayUsdc);
        // No permission from the borrower is needed to repay their debt.
        pool.repay(USDC, repayUsdc, VARIABLE_RATE, borrower);
        vm.stopPrank();

        (, uint256 debtAfter,,,,) = pool.getUserAccountData(borrower);
        assertLt(debtAfter, debtBefore, "repay must reduce borrower debt");
    }

    function test_collateralMovesOnlyViaATokenAllowance() public {
        _openTightPosition();
        address aWeth = pool.getReserveData(WETH).aTokenAddress;
        address vaultLike = makeAddr("vaultLike");

        // First repay some debt so the borrower can safely release a little collateral.
        deal(USDC, vaultLike, 300e6);
        vm.startPrank(vaultLike);
        IERC20(USDC).approve(address(pool), 300e6);
        pool.repay(USDC, 300e6, VARIABLE_RATE, borrower);
        vm.stopPrank();

        uint256 pull = 0.1 ether;

        // Without an allowance, transferFrom the aToken reverts.
        vm.prank(vaultLike);
        vm.expectRevert();
        IERC20(aWeth).transferFrom(borrower, vaultLike, pull);

        // Borrower grants the aToken allowance (the opt-in).
        vm.prank(borrower);
        IERC20(aWeth).approve(vaultLike, type(uint256).max);

        uint256 wethBefore = IERC20(WETH).balanceOf(vaultLike);
        vm.startPrank(vaultLike);
        IERC20(aWeth).transferFrom(borrower, vaultLike, pull); // pull aTokens
        pool.withdraw(WETH, type(uint256).max, vaultLike); // burn vault-held aTokens for underlying
        vm.stopPrank();

        assertGt(IERC20(WETH).balanceOf(vaultLike) - wethBefore, 0, "vault should receive WETH");
    }

    function test_cannotWithdrawCollateralThatBreaksHealth() public {
        _openTightPosition();
        // Borrower cannot pull all collateral while debt is outstanding — HF check reverts.
        vm.prank(borrower);
        vm.expectRevert();
        pool.withdraw(WETH, type(uint256).max, borrower);
    }
}
