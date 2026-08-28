// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @notice Small helpers for Health-Factor / basis-point math.
/// @dev Aave's `healthFactor` is WAD-scaled (1e18). Risk params use basis points (1e4).
library HealthMath {
    uint256 internal constant WAD = 1e18;
    uint256 internal constant BPS = 1e4;

    /// @notice Convert a basis-point HF (e.g. 12500 = 1.25) to WAD (1.25e18).
    function bpsToWad(uint256 bps) internal pure returns (uint256) {
        return bps * 1e14;
    }

    /// @notice Debt-to-collateral leverage proxy in WAD. Higher = more levered.
    /// Returns 0 when there is no collateral (avoids divide-by-zero).
    function leverageWad(uint256 debtBase, uint256 collateralBase) internal pure returns (uint256) {
        if (collateralBase == 0) return 0;
        return (debtBase * WAD) / collateralBase;
    }
}
