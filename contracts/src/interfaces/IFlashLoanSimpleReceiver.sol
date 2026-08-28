// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @notice Aave V3 flash-loan (simple) receiver callback.
interface IFlashLoanSimpleReceiver {
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external returns (bool);
}
