# docs/evals

The public copy of the EVALS page. Everything here is generated: do not edit it.

| File | What it is |
|---|---|
| `index.html` | The page as one self-contained file. Open it from the file system, or serve it from GitHub Pages |
| `figures/<figure>.<theme>.png` | The README's figures (`labels-buy`, `roc`, and `read-less` only when the results file carries the opt-in `fleet` block), light and dark, 832 CSS pixels wide at 2× |

To rebuild, in `ui/`: `npm run export:evals`. The page and the script are `ui/src/evals/` and
`ui/scripts/export-evals.mjs`; the numbers are the newest file in `bench/results/`.

In a README, a figure follows the reader's theme:

```html
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/evals/figures/roc.dark.png">
  <img src="docs/evals/figures/roc.light.png" width="832" alt="ROC curves, one per voter">
</picture>
```
