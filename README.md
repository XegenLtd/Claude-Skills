# Xegen Claude Code skills

A [Claude Code](https://code.claude.com) **plugin marketplace** — a catalog of Xegen's
skills and tooling that anyone can install with a couple of commands.

Marketplace name: **`xegen-skills`** · Repo: `XegenLtd/Claude-Skills`

## Install

In Claude Code:

```
/plugin marketplace add XegenLtd/Claude-Skills
/plugin install projectlayer-build@xegen-skills
```

The first command registers this marketplace (by its repo); the second installs a plugin
from it using `<plugin-name>@xegen-skills`.

Refresh later with `/plugin marketplace update xegen-skills`.

## Plugins

### `projectlayer-build`

Authors development plans from a [ProjectLayer.app](https://projectlayer.app) task's
description and builds the code from a plan, keeping the task's status in sync via the
ProjectLayer REST API.

**Setup:** set a **write-scoped** API token (ProjectLayer → Settings):

```bash
export PROJECTLAYER_API_TOKEN=pl_live_xxx
# optional, for self-hosted instances:
# export PROJECTLAYER_BASE_URL=https://your-domain/api/v1
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
