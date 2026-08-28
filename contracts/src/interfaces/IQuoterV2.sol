// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @notice Uniswap V3 QuoterV2 subset. These functions are NOT view (they execute a swap and revert
/// internally to measure), so call them from an off-chain `eth_call` or a test, never on-chain in prod.
/// Address on Arbitrum One: 0x61fFE014bA17989E743c5F6cB21bF9697530B21e.
interface IQuoterV2 {
    struct QuoteExactOutputSingleParams {
        address tokenIn;
        address tokenOut;
        uint256 amount; // desired amountOut
        uint24 fee;
        uint160 sqrtPriceLimitX96;
    }

    function quoteExactOutputSingle(QuoteExactOutputSingleParams calldata params)
        external
        returns (uint256 amountIn, uint160 sqrtPriceX96After, uint32 initializedTicksCrossed, uint256 gasEstimate);
}
