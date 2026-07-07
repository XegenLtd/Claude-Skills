# Xegen Claude Code skills

A [Claude Code](https://code.claude.com) **plugin marketplace** — a catalog of Xegen's
skills and tooling that anyone can install with a couple of commands.

Marketplace name: **`xegen-skills`** · Repo: `XegenLtd/Claude-Skills`

## Install

### Quick (commands)

In Claude Code:

```
/plugin marketplace add XegenLtd/Claude-Skills
/plugin install projectlayer-build@xegen-skills
```

The first command registers this marketplace (by its repo); the second installs a plugin
from it using `<plugin-name>@xegen-skills`.

Refresh later with `/plugin marketplace update xegen-skills`.

### Using the Claude Code UI

Prefer to browse and click? Use the interactive plugin manager:

1. Run `/plugin` to open the plugin manager. It has four tabs — **Discover**, **Installed**,
   **Marketplaces**, **Errors** — cycle with **Tab** (or **Shift+Tab** to go back).
2. Go to the **Marketplaces** tab and choose **Add marketplace**. Enter the repo:
   ```
   XegenLtd/Claude-Skills
   ```
3. Switch to the **Discover** tab, find **projectlayer-build**, and press **Enter** to open
   its details (you'll see the skills it adds and its context cost).
4. Choose an install scope — **User** (all your projects), **Project** (shared with
   collaborators on this repo), or **Local** (just you, this repo) — and confirm.
5. Run `/reload-plugins` to activate it.

From the **Marketplaces** tab you can also update or remove the marketplace later.

## Plugins

### `projectlayer-build`

Authors development plans from a [ProjectLayer.app](https://projectlayer.app) task's
description and builds the code from a plan, keeping the task's status in sync via the
ProjectLayer REST API.

**Setup:** set a **write-scoped** API token from your
[projectlayer.app](https://projectlayer.app) account (Settings → API):

```bash
export PROJECTLAYER_API_TOKEN=pl_live_xxx
```

Requires Python 3 (standard library only). The skill is model-invoked — just ask, e.g.
*"Write a plan for RSPH-23 and push it back"* or *"Build RSPH-14"*.

---

## Repository layout

```
Claude-Skills/                          ← this repo (the marketplace)
├── .claude-plugin/
│   └── marketplace.json                ← the catalog: lists every plugin
└── plugins/                            ← one folder per plugin (metadata.pluginRoot)
    └── projectlayer-build/
        ├── .claude-plugin/
        │   └── plugin.json             ← this plugin's manifest
        └── skills/
            └── projectlayer-build/
                ├── SKILL.md
                └── scripts/...
```

Only `plugin.json` / `marketplace.json` go inside a `.claude-plugin/` folder. Everything
else (`skills/`, `scripts/`, …) sits at the plugin root.
