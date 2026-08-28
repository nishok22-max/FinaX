// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {IPool} from "../src/interfaces/IPool.sol";
import {IPoolAddressesProvider} from "../src/interfaces/IPoolAddressesProvider.sol";
import {IPriceOracleGetter} from "../src/interfaces/IPriceOracleGetter.sol";
import {IQuoterV2} from "../src/interfaces/IQuoterV2.sol";

/// @notice Shared Arbitrum One mainnet-fork setup + position helpers for the vault test suite.
/// @dev Requires ARBITRUM_RPC_URL (and optionally FORK_BLOCK) in the environment / .env.
abstract contract ForkBase is Test {
    // --- Arbitrum One address book (mirror of addresses.json) ---
    address internal constant POOL_ADDRESSES_PROVIDER = 0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb;
    address internal constant SWAP_ROUTER = 0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45;
    address internal constant QUOTER = 0x61fFE014bA17989E743c5F6cB21bF9697530B21e;
    address internal constant WETH = 0x82aF49447D8a07e3bd95BD0d56f35241523fBab1;
    address internal constant USDC = 0xaf88d065e77c8cC2239327C5EDb3A432268e5831;
    uint24 internal constant FEE = 500;
    uint256 internal constant VARIABLE_RATE = 2;

    IPool internal pool;
    IPriceOracleGetter internal oracle;
    IQuoterV2 internal quoter;

    address internal borrower;
    uint256 internal borrowerPk;

    function setUp() public virtual {
        string memory rpc = vm.envString("ARBITRUM_RPC_URL");
        uint256 forkBlock = vm.envOr("FORK_BLOCK", uint256(0));
        if (forkBlock > 0) {
            vm.createSelectFork(rpc, forkBlock);
        } else {
            vm.createSelectFork(rpc);
        }

        IPoolAddressesProvider provider = IPoolAddressesProvider(POOL_ADDRESSES_PROVIDER);
        pool = IPool(provider.getPool());
        oracle = IPriceOracleGetter(provider.getPriceOracle());
        quoter = IQuoterV2(QUOTER);

        (borrower, borrowerPk) = makeAddrAndKey("borrower");
        vm.label(address(pool), "AavePool");
        vm.label(SWAP_ROUTER, "UniV3Router");
        vm.label(WETH, "WETH");
        vm.label(USDC, "USDC");
    }

    /// @notice Open a WETH-collateral / USDC-debt position for `borrower`.
    function _openPosition(uint256 wethCollateral, uint256 usdcDebt) internal {
        deal(WETH, borrower, wethCollateral);
        vm.startPrank(borrower);
        IERC20(WETH).approve(address(pool), type(uint256).max);
        pool.supply(WETH, wethCollateral, borrower, 0);
        if (usdcDebt > 0) {
            pool.borrow(USDC, usdcDebt, VARIABLE_RATE, 0, borrower);
        }
        vm.stopPrank();
    }

    function _healthFactor(address user) internal view returns (uint256 hf) {
        (,,,,, hf) = pool.getUserAccountData(user);
    }

    /// @notice Quote the exact WETH input needed to receive `usdcOut` USDC on Uniswap V3.
    function _quoteWethIn(uint256 usdcOut) internal returns (uint256 amountIn) {
        (amountIn,,,) = quoter.quoteExactOutputSingle(
            IQuoterV2.QuoteExactOutputSingleParams({
                tokenIn: WETH,
                tokenOut: USDC,
                amount: usdcOut,
                fee: FEE,
                sqrtPriceLimitX96: 0
            })
        );
    }
}
