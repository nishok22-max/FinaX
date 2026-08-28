"""Phase 3 exit-criteria: read HF, prices, and a live quote from a fork through typed clients.

Requires ``web3`` importable and a reachable Arbitrum One RPC / anvil fork (see conftest). Skips
cleanly otherwise. Point ``ARBITRUM_RPC_URL`` at ``anvil --fork-url ...`` for the demo run.
"""
from __future__ import annotations

import pytest

from app.config.arbitrum import TOKENS

pytestmark = pytest.mark.asyncio

# A large, always-active Aave V3 borrower can be pinned here for a deterministic HF read;
# left blank so the test only asserts the call shape unless an address is provided.
SAMPLE_BORROWER = ""


async def test_connected(chain_client) -> None:  # type: ignore[no-untyped-def]
    assert await chain_client.chain_id() == 42161


async def test_reserve_info_weth(chain_client) -> None:  # type: ignore[no-untyped-def]
    from app.chain.aave import AaveClient

    aave = AaveClient(chain_client)
    info = await aave.get_reserve_info(TOKENS["WETH"])
    assert int(info.aToken_address, 16) != 0
    assert info.decimals == 18
    assert 0 < info.liq_threshold_bps <= 10_000


async def test_user_account_data_shape(chain_client) -> None:  # type: ignore[no-untyped-def]
    from app.chain.aave import AaveClient

    borrower = SAMPLE_BORROWER or "0x0000000000000000000000000000000000000001"
    aave = AaveClient(chain_client)
    uad = await aave.get_user_account_data(borrower)
    assert uad.total_collateral_base >= 0
    assert uad.total_debt_base >= 0
    if uad.has_debt:
        assert uad.hf > 0
    else:
        assert uad.hf == float("inf")


async def test_live_quote_weth_to_usdc(chain_client) -> None:  # type: ignore[no-untyped-def]
    from app.chain.uniswap import UniswapClient

    uni = UniswapClient(chain_client)
    quote = await uni.quote_exact_output_single(
        token_in=TOKENS["WETH"],
        token_out=TOKENS["USDC"],
        amount_out=1_000 * 10**6,  # 1,000 USDC out
    )
    assert quote.amount_in > 0  # some WETH is required
    assert quote.fee == 500


async def test_aave_oracle_weth_price(chain_client) -> None:  # type: ignore[no-untyped-def]
    from app.chain.oracle import OracleClient

    oracle = OracleClient(chain_client)
    price = await oracle.get_asset_price(TOKENS["WETH"])
    assert price.price > 0
    assert price.decimals == 8
