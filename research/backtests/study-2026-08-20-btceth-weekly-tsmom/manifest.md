# Run Manifest: BTC/ETH weekly TSMOM

- study id: `study-2026-08-20-btceth-weekly-tsmom`
- implementation candidate SHA: `97208bbef6b60cbc9eef09613f99ae60cb4de2e8`
- preregistration SHA-256: `34fb412fbdd110cd704b31b85460d8a4116a10b22d391dfc38b20d1afef09025`
- acquisition amendment SHA-256: `026024cfe3fc45b554b3362c3f6d794772a51f241a6b19eaff707fbb5d514de2`
- runner SHA-256: `b0f9501c86ed40109874275dd53d2251dfb3e6e9c15cdda0ee276f1bfa8c1952`
- data manifest: `user_data/data/tsmom/manifest.json`
- data manifest SHA-256: `9df487802c8b859d0247d4027dae4046987090d2153eeabab54036591bbb2894`
- execution-cost model SHA-256: `effa8ab33aa35f94788e77049adf13a4a0a3f226b68771797eded866d2689dc6`
- formal run UTC: `2026-08-20T06:57:22Z`
- public data only: `TRUE`
- private API calls / credentials / orders: `NONE`
- train window: `2021-01-01..2023-12-31 UTC`
- OOS window: `2024-01-01..2026-08-19 UTC`
- primary rule: `180d own return sign, weekly next-open, equal active BTC/ETH, long-or-cash, gross <= 1x`
- baseline cost per side: `15.0 bps`
- stress cost per side: `30.0 bps`; funding `5x`
- verdict: `REJECT`
