# HyperGrok v1.0.0 launch pack

Materials for the X launch. Copy, brand assets, and the reasoning behind the
post structure.

---

## Pre-launch checklist

- [ ] **Repository is public.** As of writing it is private, so every link in
      the post 404s. `gh repo edit galleonlabs/hypergrok-trading-desk --visibility public --accept-visibility-change-consequences`
- [ ] CI and CodeQL green on `main`
- [ ] `v1.0.0` tag present
- [ ] Repository social preview set to `assets/hypergrok-orb.png`
- [ ] `BOOTSTRAP.md` renders correctly on the public URL (it is the entry point
      the post sends people to)

---

## Post copy

### Main post — short form (271 characters)

> Turn your Grok bots into a Hyperliquid trading desk.
>
> HyperGrok: 7 agents, 11 skills, one guarded order path.
>
> Most "AI trading" repos are a prompt and a private key. This one won't send an order without a SHA-256 you confirmed.
>
> Open source, MIT:
> github.com/galleonlabs/hypergrok-trading-desk

Attach `assets/hypergrok-card.png`.

### Main post — long form

> Turn your Grok bots into a Hyperliquid trading desk.
>
> HyperGrok: 7 specialist agents, 11 skills, one guarded order path — research, thesis, risk, execution, portfolio control, post-trade review.
>
> Most "AI trading" repos are a prompt and a private key. This one never sees your seed phrase, and won't send an order without a SHA-256 you confirmed.
>
> Testnet by default. Mainnet makes you say it out loud.
>
> Open source, MIT:
> github.com/galleonlabs/hypergrok-trading-desk

### Replies

These reach existing followers, not the For You feed (see *Reach* below). Use
them for depth, not for the core pitch.

**Reply 1 — the desk**

> It's seven roles, not one prompt in a trenchcoat:
>
> Desk lead — routes evidence and approvals
> Market analyst — HL structure, liquidity, OI, funding
> Onchain analyst — protocol, token, governance
> Portfolio manager — exposure, margin, protection
> Risk officer — independent sizing, hard rejects
> Execution trader — the only order path
> Trade reviewer — plan vs effect

**Reply 2 — setup**

> Setup is a paste.
>
> Grok Bot: point it at BOOTSTRAP.md. It builds the seven roles, assigns each its skills, and hands you a receipt of what's actually live. If it can't spawn Bots itself, it gives you seven labelled setup blocks instead.
>
> Grok Build / Cursor: it's an Agent Plugin. `grok --plugin-dir .` then `/crew-bootstrap`.

**Reply 3 — the guarantees**

> Why it isn't another "AI trading bot":
>
> Never touches your seed phrase — scoped API wallet only
> Plans are hash-bound, capped, expire in 30 min
> You confirm the exact SHA-256 before anything sends
> No deposits, withdrawals, transfers, bridging
> No auto-retry after a send
> Fails closed

**Reply 4 — honest limits**

> What it doesn't do:
>
> No strategy catalogue. No hosted runtime. No unattended 24/7 trading. No profitability claim.
>
> v1.0.0 verifies read-only data, sizing, planning and fail-closed execution. Live funded submission is still gated.
>
> Perps liquidate accounts. Not financial advice.

---

## Why the post is shaped this way

Derived from the published For You ranking code
([xai-org/x-algorithm](https://github.com/xai-org/x-algorithm)), read on
2026-08-16. Default action weights from
[`home-mixer/params/param.rs`](https://github.com/xai-org/x-algorithm/blob/main/home-mixer/params/param.rs):

| Action | Weight |
| --- | --- |
| Share via copy link | **20.0** |
| Reply from a mutual (boost) | **15.0** |
| Reply / Quote / Share via DM | **5.0** |
| Follow author | 4.0 |
| Share | 2.0 |
| Repost | 1.0 |
| **Favourite (like)** | **0.5** |
| Click | 0.4 |
| **Open link** | **0.2** |
| Photo expand / video open / VQV | 0.05 |
| Not interested | −43.2 |
| Block author | −31.2 |
| Mute author | −58.8 |
| Report | −234.0 |

The weights scale *predicted probabilities* of each action, not raw engagement
counts — the README is explicit that reading them as count multipliers is wrong.
They still tell you which behaviours the ranker is trying to produce.

What follows for this post:

1. **Copy-link shares dominate.** At 20.0 they are worth 40× a like. A repo
   link pasted into a Discord or Slack is exactly that action. The post should
   read as something worth forwarding, not something worth applauding.

2. **Put the link in the main post, not a reply.** Common advice says bury
   links to dodge a penalty. There is no penalty in the code — `OpenLinkWeight`
   is simply low (0.2). Meanwhile `OONRetweetReplyFilter` removes replies from
   accounts the viewer does not follow, so a link parked in a reply reaches
   almost nobody new. Main post wins.

3. **Tweet 1 carries the whole payload.** Same filter: thread replies do not
   travel out-of-network. Anything essential that lives in reply 3 is invisible
   to strangers. Hence a self-contained first post.

4. **Optimise for replies and quotes, not likes.** Both are 5.0 against a
   like's 0.5. The "a prompt and a private key" line is deliberately arguable —
   it gives people something to quote or push back on.

5. **Answer your mutuals fast.** A reply from a bidirectional follow carries a
   15.0 boost, the second-largest positive weight in the file.

6. **Make it follow-worthy.** `FollowAuthorWeight` is 4.0, 8× a like.

7. **Post once.** `AuthorDiversityDecay` is 0.5 with a 0.25 floor — your second
   post to the same viewer is halved, the third quartered.

8. **One image is enough.** Media actions are 0.05 and `DwellWeight` is 0.0.
   The graphic earns attention from humans, not from the ranker.

9. **Restraint is algorithmically correct.** Mute is −58.8 and report is −234.0.
   Hype-shaped crypto launch copy draws exactly those actions from strangers.
   The measured, limits-included tone is the safer play as well as the honest one.

10. **48-hour window.** `AgeFilter` drops posts older than 48 hours from
    candidate retrieval.

---

## Assets

| File | Size | Use |
| --- | --- | --- |
| `assets/hypergrok-card.png` | 3200×1800 | Main post image (16:9) |
| `assets/hypergrok-orb.png` | 1024×1024 | Avatar, repo social preview |
| `assets/orb.svg` | vector | Source mark |
| `assets/card.html`, `assets/orbsq.html` | — | Render sources |

Re-render with headless Chrome:

```bash
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
"$CHROME" --headless --disable-gpu --hide-scrollbars --force-device-scale-factor=2 \
  --window-size=1600,900 --screenshot=hypergrok-card.png assets/card.html
"$CHROME" --headless --disable-gpu --hide-scrollbars \
  --window-size=1024,1024 --screenshot=hypergrok-orb.png assets/orbsq.html
```

### Palette

Sampled from the live Hyperliquid app on 2026-08-16, not from a secondhand
brand list.

| Token | Hex |
| --- | --- |
| Ground | `#0F1A1F` |
| Mint (bright) | `#97FCE4` |
| Teal (accent) | `#50D2C1` |
| Long | `#1FA67D` |
| Short | `#ED7088` |
| Text | `#F6FEFD` |
| Muted | `#949E9C` |

### Mascot note

The orb is a mint recolour of the Grok mascot silhouette — a community riff on
xAI's mark, not an original character and not an endorsement by xAI or
Hyperliquid. Swap it if that reading is a problem.

### Grok Imagine prompts

If a rendered version is wanted instead of the vector:

> A cute glossy 3D sphere mascot, smooth matte-glass finish, mint green —
> bright #97FCE4 highlight fading through #50D2C1 to deep teal #15625E at the
> edges. Two large dark rounded-capsule slit eyes tilted ~16° clockwise, set on
> a gently rising diagonal across the upper face. Soft studio key light from
> upper left, bright specular highlight, subtle rim light. Near-black teal
> background #0F1A1F, soft mint glow, gentle contact shadow. Minimal, premium
> product render, centred, 16:9.

Desk variant:

> Same mint sphere mascot wearing a slim dark headset, floating in front of
> faint mint candlestick charts on a near-black teal background. Cute, calm,
> restrained. No text.

---

## Claims to avoid

- **"Give Grok Bot the repo and it spins up the agents."** Overstates it. Grok
  Bot has no public API for silently creating Bots; `crew-bootstrap` verifies
  the installed product and falls back to labelled setup blocks. The repo
  deliberately refuses the one-click claim — the copy above matches that.
- **Any profitability, alpha, or autonomous-trading claim.** v1.0.0 makes none,
  and live funded submission has not been exercised.
- **"Audited."** It is not.
