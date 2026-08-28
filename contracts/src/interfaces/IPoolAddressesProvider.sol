// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @notice Minimal Aave V3 PoolAddressesProvider interface.
interface IPoolAddressesProvider {
    function getPool() external view returns (address);

    function getPriceOracle() external view returns (address);
}
