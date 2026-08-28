// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ForkBase} from "./ForkBase.t.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {LiquidationShieldVault} from "../src/LiquidationShieldVault.sol";

/// @title Phase 2 / Sprint 0 Gate B — Sizing parity
/// @notice Cross-checks the closed-form Δd* against the real on-chain HF outcome: size a repayment
/// to lift HF to a chosen target, execute on the fork, and assert HF lands at/above target without
/// massive overshoot. Demonstrates "math proposes, simulation validates, Solidity enforces".
contract SizingParityTest is ForkBase {
    LiquidationShieldVault internal vault;
    address internal keeper;

    function setUp() public override {
        super.setUp();
        keeper = makeAddr("keeper");
        vault = new LiquidationShieldVault(POOL_ADDRESSES_PROVIDER, SWAP_ROUTER, keeper);

        _openPosition(2 ether, 0);
        (,, uint256 availableBase,,,) = pool.getUserAccountData(borrower);
        uint256 usdcAmount = (availableBase * 1e6 * 70) / (oracle.getAssetPrice(USDC) * 100);
        vm.prank(borrower);
        pool.borrow(USDC, usdcAmount, VARIABLE_RATE, 0, borrower);

        address aWeth = pool.getReserveData(WETH).aTokenAddress;
        vm.prank(borrower);
        IERC20(aWeth).approve(address(vault), type(uint256).max);
    }

    function _signed(uint256 nonce) internal view returns (LiquidationShieldVault.RiskParams memory p, bytes memory sig) {
        address[] memory allowed = new address[](1);
        allowed[0] = WETH;
        p = LiquidationShieldVault.RiskParams({
            borrower: borrower,
            hfTriggerBps: 11500,
            hfTargetBaseBps: 10500,
            volCoeffK: 0,
            hfTargetMaxBps: 40000,
            maxSlippageBps: 300,
            maxCostBps: 500,
            allowedCollaterals: allowed,
            nonce: nonce,
            deadline: block.timestamp + 1 hours
        });
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(borrowerPk, vault.hashRiskParams(p));
        sig = abi.encodePacked(r, s, v);
    }

    /// @dev Δd* (in USDC) to lift HF from current to `targetBps`, per PRD §7:
    ///   Δd_base = (target·D − C·LT) / (target − (1+f)·LT), converted to USDC units.
    function _sizeRepay(uint256 targetBps) internal view returns (uint256 repayUsdc) {
        (uint256 collatBase, uint256 debtBase,, uint256 liqThreshold,,) = pool.getUserAccountData(borrower);
        uint256 ltWad = liqThreshold * 1e14; // bps -> wad
        uint256 targetWad = targetBps * 1e14;
        uint256 onePlusF = 1e18 + 1e16; // 1% bundled cost factor

        uint256 num = targetWad * debtBase - collatBase * ltWad; // base * 1e18 (target must exceed HF)
        uint256 denom = targetWad - (onePlusF * ltWad) / 1e18; // wad
        uint256 deltaBase = num / denom; // base units (1e8 USD)

        repayUsdc = (deltaBase * 1e6) / oracle.getAssetPrice(USDC);
        repayUsdc = (repayUsdc * 103) / 100; // small over-repay so HF lands at/above target
    }

    function test_sizingReachesTarget() public {
        uint256 hfBefore = _healthFactor(borrower);
        uint256 targetBps = hfBefore / 1e14 + 2000; // aim ~+0.20 HF above current

        uint256 repayUsdc = _sizeRepay(targetBps);
        uint256 outNeeded = repayUsdc + (repayUsdc * 5) / 10000;
        uint256 amountInMax = (_quoteWethIn(outNeeded) * 102) / 100;

        (LiquidationShieldVault.RiskParams memory p, bytes memory sig) = _signed(1);

        vm.prank(keeper);
        vault.executeProtection(p, sig, USDC, repayUsdc, WETH, amountInMax, FEE, targetBps);

        uint256 hfAfter = _healthFactor(borrower);
        uint256 targetWad = targetBps * 1e14;
        assertGe(hfAfter, targetWad, "HF must reach target");
        assertLe(hfAfter, (targetWad * 112) / 100, "HF should not massively overshoot target");
    }
}
