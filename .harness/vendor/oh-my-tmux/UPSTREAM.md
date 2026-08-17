# Oh my tmux! vendored snapshot

Upstream: https://github.com/gpakosz/.tmux
Snapshot branch: `master`
Retrieved: 2026-07-29

Awoki vendors the upstream main configuration and license files so container
startup never downloads or executes remote content. Integrity is pinned by the
following SHA-256 digests:

```text
7e17717cd844189eb2501c31690035a761bb31a10214e82d51e7eafa0eea49f4  .tmux.conf
41a7d6aedab5f39f5a1350d060b5409026d8ab7cffa0de8cc662a71d24a3df7c  .tmux.conf.local
f4c34427d3e29630cece9bca418eda48ac298867630cd6698d73e0f04642d1d0  LICENSE.MIT
ee820ff0db4ce628569e0975ac27dc926052a9f85d102b101edb104311ef4d90  LICENSE.WTFPLv2
```

The upstream `.tmux.conf.local` is retained as a reference sample. Awoki uses
`.harness/config/tmux.conf.local` as its container customization layer.
