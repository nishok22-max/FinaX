// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @notice Aave V3 price oracle getter. Prices are denominated in the oracle base currency
/// (USD with 8 decimals on the Arbitrum market).
interface IPriceOracleGetter {
    function BASE_CURRENCY_UNIT() external view returns (uint256);

    function getAssetPrice(address asset) external view returns (uint256);
}
