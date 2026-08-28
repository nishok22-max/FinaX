// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @notice Uniswap V3 SwapRouter02 subset. Note: SwapRouter02 params carry NO `deadline` field
/// (unlike the original SwapRouter). Address on Arbitrum One: 0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45.
interface ISwapRouter02 {
    struct ExactOutputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 amountOut;
        uint256 amountInMaximum;
        uint160 sqrtPriceLimitX96;
    }

    /// @return amountIn The amount of tokenIn actually spent to receive `amountOut`.
    function exactOutputSingle(ExactOutputSingleParams calldata params) external payable returns (uint256 amountIn);
}
