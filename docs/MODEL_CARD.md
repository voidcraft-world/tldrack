# Model card · Laya on the agent rack

## Model details
- Base model: [`convaiinnovations/laya`](https://huggingface.co/convaiinnovations/laya), by Convai Innovations, Apache-2.0. A ModernBERT-large encoder (`answerdotai/ModernBERT-large`, Apache-2.0) with a decision head: 421M parameters. It is not a text generator: it reads a state and a question and returns a probability.
- tldrack downloads the base model from Hugging Face on install and runs it on the operator's machine. It works with no fine-tune at all; that is the first run.
- The question it is asked on the `agent_open_work` surface, and the cuts the rack draws ⚠️ and ✅ at: `bridge/tldrack/open_work.py`.

## The fine-tune
- `laya-lab` fine-tunes the base model on one machine, for one operator. The training rows are closing messages of coding-agent turns from that operator's own sessions, each labelled open or done: most labels come from a teacher model that read the message alone, and the operator's own grades override the teacher wherever they exist.
- Half of the operator's grades are held in reserve and never trained on: the rows a cut or a promotion is measured against.
- **No trained weights ship with tldrack.** A fine-tune, its training rows, the operator's grades and the decision ledger stay in the operator's data directory. This repository holds the code that trains one, the public golden suite, and aggregate numbers.

## Intended use
- Reads the closing message of a coding agent's turn and answers one yes/no question: does this message say work is still open. The agent rack draws ⚠️ or ✅ from the probability at two cuts.
- It helps a person decide where to look first. It is not a reason to close, kill or auto-answer a session.
- Out of scope: any text but an English turn-ending message, and any decision with no person in the loop.

## Limits
- It judges the message, not the work: a turn that says it is done and is not reads as done.
- Most training labels are a teacher model's, so the fine-tune inherits the teacher's reading wherever the operator has not graded.
- A fine-tune is fitted to one operator's agents, projects and habits of phrasing. Its numbers do not carry to another operator; the base model's numbers on the public suite do.
- The held-out sets are small. Read every number with its row count and its interval.
- A message past the model's window is judged from its head and tail, not in full.
- The public golden suite is small and invented (`VERIFIER.md` says how it was made). The question's wording was chosen on the first 34 cases of the public suite, so their numbers flatter the base model.
