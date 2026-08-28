// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Script} from "forge-std/Script.sol";
import {console2} from "forge-std/console2.sol";
import {LiquidationShieldVault} from "../src/LiquidationShieldVault.sol";

/// @notice Deploys LiquidationShieldVault. Configure via env:
///   DEPLOYER_PRIVATE_KEY, KEEPER_ADDRESS. Addresses default to Arbitrum One.
contract Deploy is Script {
    address constant POOL_ADDRESSES_PROVIDER = 0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb;
    address constant SWAP_ROUTER = 0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45;

    function run() external returns (LiquidationShieldVault vault) {
        uint256 pk = vm.envUint("DEPLOYER_PRIVATE_KEY");
        address keeper = vm.envAddress("KEEPER_ADDRESS");

        vm.startBroadcast(pk);
        vault = new LiquidationShieldVault(POOL_ADDRESSES_PROVIDER, SWAP_ROUTER, keeper);
        vm.stopBroadcast();

        console2.log("LiquidationShieldVault:", address(vault));
        console2.log("Pool:", address(vault.POOL()));
        console2.log("Keeper:", vault.keeper());
    }
}
