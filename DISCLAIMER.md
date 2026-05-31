# Disclaimer

**By using this software you agree to the following.** If you do not
agree, do not use it.

## Not advice

This software is provided for **educational and research purposes
only**. Nothing in this repository, including its source code,
documentation, examples, agent prompts, tool descriptions, and any
output it produces, constitutes:

- Investment advice
- Trading advice
- Financial advice
- Legal advice
- Tax advice

The authors and contributors are not registered investment advisors,
broker-dealers, or financial professionals.

## Trading involves substantial risk

Trading prediction markets — including but not limited to those on
Kalshi — involves **substantial risk of loss**. You can lose some or
all of the money you put into a position. Past performance does not
predict future results. Liquidity, settlement, and contract terms vary
by market and can change without notice.

You should not trade with money you cannot afford to lose. You should
understand each market's terms before placing an order.

## AI-driven trading has additional unique risks

This server lets a large language model invoke trading tools on your
behalf. That introduces failure modes that don't exist in human-only
trading:

- **Model mistakes.** LLMs misread tickers, confuse YES/NO sides,
  invert prices, hallucinate market state, and fabricate plausible-
  sounding rationales for wrong actions.
- **Prompt injection.** Anything an LLM reads (a market title, a news
  article, a Discord message it was asked to summarize, etc.) can
  contain instructions that override what you actually asked. The
  model may then issue trades you didn't intend.
- **Runaway loops.** An agentic loop can place many orders in rapid
  succession before you notice — especially if it's chasing a "fix"
  for a perceived problem.
- **Stale state.** The model's view of the world is delayed; it may
  trade against quotes that no longer exist.
- **No common sense.** The model has no intrinsic understanding of the
  monetary value at stake. To it, "buy 100" and "buy 10000" are
  semantically equivalent — just digits.

Server-side safety controls (`KALSHI_TRADING_ENABLED`,
`MCP_MAX_ORDER_SIZE_USD`, `MCP_DAILY_LIMIT_USD`,
`MCP_MAX_CONTRACTS_PER_ORDER`, `MCP_CASH_RESERVE_USD`) are a
defense-in-depth layer, **not a guarantee**. They reduce the blast
radius of mistakes but cannot prevent every adverse outcome. You are
responsible for configuring them to match your risk tolerance.

## You are responsible

You are solely responsible for:

- Any orders placed through this software, whether by you or by an AI
  agent acting on your behalf
- All gains, losses, fees, taxes, and other financial consequences of
  those orders
- Configuring and verifying the safety controls
- Securing your API keys, OAuth credentials, and deployment
  infrastructure
- Complying with all applicable laws, regulations, and Kalshi's terms
  of service in your jurisdiction
- Verifying that prediction-market trading is legal where you live and
  for your circumstances

## No warranty

This software is provided "AS IS", without warranty of any kind,
express or implied. The full text of the MIT License in
[LICENSE](LICENSE) applies.

The authors and contributors:

- Make no representations about the accuracy, reliability,
  completeness, or timeliness of the software
- Make no representations that the software will be free of bugs,
  errors, security vulnerabilities, or downtime
- Are not liable for any direct, indirect, incidental, special,
  consequential, or exemplary damages — including but not limited to
  trading losses, missed opportunities, account suspensions, or
  regulatory consequences

## Test in demo, always

Kalshi provides a separate demo environment at
[demo.kalshi.co](https://demo.kalshi.co). Use it. The server defaults
to `KALSHI_ENV=demo` and refuses to start in `prod` mode without an
explicit `KALSHI_ALLOW_PROD=1` for exactly this reason.

Before flipping any of the safety gates off, you should:

1. Have run the full suite of read tools against demo and confirmed
   results make sense
2. Have run several end-to-end prepare/confirm cycles against demo
3. Have read and understood every safety control's effective behavior
4. Have a clear risk budget you're willing to lose

## Reporting issues

If you discover a safety control that doesn't behave as documented, or
a way to bypass the trading-disabled gate, please report it privately
per [SECURITY.md](SECURITY.md). Do not file public issues for safety
or auth bugs.

---

By cloning, forking, deploying, or otherwise using this software, you
acknowledge that you have read and understood this disclaimer.
