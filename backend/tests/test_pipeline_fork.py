"""Phase 4 fork-backed checks — exercise the decision modules against live Arbitrum state.

Requires ``web3`` importable and a reachable RPC/anvil fork (see conftest); skips otherwise.
A full end-to-end rescue on a synthetic position is the Phase 6 e2e test (needs anvil cheatcodes
to open a position); here we validate the read/quote wiring that the assessment pipeline depends
on — reserve config decode (incl. liquidation bonus), live oracle prices, and the selector's
QuoterV2-driven cost metrics.
"""
from __future__ import annotations

import pytest

from app.config.arbitrum import TOKENS

pytestmark = pytest.mark.asyncio


async def test_reserve_bonus_and_threshold_decoded(chain_client) -> None:  # type: ignore[no-untyped-def]
    from app.chain.aave import AaveClient

    aave = AaveClient(chain_client)
    weth = await aave.get_reserve_info(TOKENS["WETH"])
    assert 0 < weth.liq_threshold_bps <= 10_000
    assert weth.liq_bonus_bps >= 0  # WETH typically ~5% (500 bps)


async def test_selector_ranks_weth_for_usdc_debt(chain_client) -> None:  # type: ignore[no-untyped-def]
    from app.chain.aave import AaveClient
    from app.chain.erc20 import ERC20Client
    from app.chain.oracle import OracleClient
    from app.chain.uniswap import UniswapClient
    from app.core.selector import CollateralSelector

    selector = CollateralSelector(
        AaveClient(chain_client),
        UniswapClient(chain_client),
        OracleClient(chain_client),
        ERC20Client(chain_client),
    )
    # No real borrower/allowance here, so eligibility is False, but the cost metrics are real.
    ranked = await selector.evaluate(
        borrower=TOKENS["WETH"],  # arbitrary address; allowance will read 0
        vault_address=TOKENS["USDC"],
        debt_asset=TOKENS["USDC"],
        out_needed=1_000 * 10**6,  # 1,000 USDC
        candidate_collaterals=[TOKENS["WETH"]],
    )
    assert len(ranked) == 1
    choice = ranked[0]
    assert choice.amount_in > 0                 # real QuoterV2 result
    assert choice.out_needed_value_base > 0     # real oracle valuation
    assert choice.slippage_cost_bps >= 0
