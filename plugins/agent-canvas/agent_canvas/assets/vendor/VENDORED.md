# Vendored files

Third-party code this plugin serves from its own origin. Each file is a release's published bytes,
never edited. Why a file is vendored, and how it is checked when served: `agent_canvas/vendor.py`.

## Mermaid 12.2.0

| | |
|---|---|
| File | `mermaid/mermaid.min.js.gz`: `package/dist/mermaid.min.js`, compressed with `gzip -9 -n` |
| License | MIT, `mermaid/LICENSE` (the bundled dependencies' notices are inside the bundle) |
| Source | `https://registry.npmjs.org/mermaid/-/mermaid-12.2.0.tgz` |
| npm integrity | `sha512-w7nr/SFrLO7BaVTs3dQTvYSV6nYSBj0Yy9DjBhsIJtAcHqRzXSq4Pg32ujHZ/AdWsAMNEvvhbm5dt9E2tOiHzw==` |
| sha256 of `mermaid.min.js` | `a8a6a6f805b074a8261a2825c86f99c8054acb3b35f50bb7e24a4f9f2c3c3b9f` |

## Updating

Pick a release at least two weeks old. In an empty scratch directory:

```bash
V=12.2.0
npm view mermaid@$V dist.tarball dist.integrity
curl -sSfLO "https://registry.npmjs.org/mermaid/-/mermaid-$V.tgz"
echo "sha512-$(openssl dgst -sha512 -binary mermaid-$V.tgz | openssl base64 -A)"   # must equal dist.integrity
tar -xzf mermaid-$V.tgz package/dist/mermaid.min.js package/LICENSE
shasum -a 256 package/dist/mermaid.min.js
gzip -9 -n -c package/dist/mermaid.min.js > <plugin>/agent_canvas/assets/vendor/mermaid/mermaid.min.js.gz
cp package/LICENSE <plugin>/agent_canvas/assets/vendor/mermaid/LICENSE
```

Then set `MERMAID_VERSION` and `MERMAID_SHA256` in `agent_canvas/vendor.py`, and the version, integrity
and sha256 in this file. `tests/test_vendor.py` fails until all three agree with the bytes.
