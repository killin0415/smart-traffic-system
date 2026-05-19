# Project Rules

## Python 環境操作

- **一律使用 `uv`** 執行 Python 相關指令（`uv run`、`uv sync`、`uv add`、`uv pip ...`）
- **禁止**直接呼叫 `.venv/Scripts/python.exe` 或 `.venv/bin/python`
- 跑腳本：`uv run python main.py`
- 跑測試：`uv run pytest`
- 新增/移除套件：`uv add <pkg>` / `uv remove <pkg>`（不要手動改 `pyproject.toml` 再 `pip install`）

## 適用範圍

`backend/multiagent-service/` 使用 `uv`（venv 在 `backend/multiagent-service/.venv/`，pyproject 在同目錄）。在該目錄下執行 `uv run ...` 即會自動用正確的 venv，不需要手動 activate。

## OpenSpec 工作流：propose / apply 完成後自動 review

每當以下任一 skill 完成執行，**必須**在回報使用者「完成」之前，自動透過 `Agent` 工具開一個 sub-agent 做 review，不要等使用者開口要求：

- `opsx:propose` / `openspec-propose`
- `opsx:apply` / `openspec-apply-change`
- `opsx:ff`（一次 propose 全套 artifacts）
- `opsx:continue`（產下一個 artifact）

### Review 規則

**propose / continue / ff 完成後** → 派 `general-purpose` sub-agent，prompt 必須自包含（傳入 change 名稱、`openspec/changes/<change>/` 路徑），要求檢查：

- spec deltas 之間的內部矛盾（例如一個 scenario 過濾掉某條件、另一個 scenario 假設該條件會走到）
- BREAKING 變動是否在 `proposal.md` 明確標註為 BREAKING（特別是 wire schema、DB schema、env var 變動）
- proposal/design/tasks 中引用的檔案路徑、git commit hash、函數名稱是否真的存在於 codebase
- `tasks.md` 與 `design.md` 是否一致（例如 design 列了測試檔但 tasks 沒任務、反之亦然）
- 測試 / 範圍數量算式是否合理
- 即使 `openspec validate --strict` 通過，仍要看 spec 文字本身是否清楚

**apply 完成後** → 派 `superpowers:code-reviewer` sub-agent，傳入 change 資料夾路徑與 diff 範圍（`git diff <base>..HEAD`），要求驗證：

- 實作是否符合 change artifacts 的 spec / tasks
- 是否遵守本專案規則（特別是 Python `uv` 規則：不能出現 `.venv/Scripts/python.exe`、`.venv/bin/python`、手動 `pip install`）
- 是否有缺漏的測試、未處理的 task

### 結果呈現

把 sub-agent 回報的內容**整理過**再給使用者，不要原文 paste。用 🔴 / 🟡 / 🟢 嚴重度分級，每條一句話總結 + 必要時引用檔案行號。然後問使用者要怎麼處理（修哪些、忽略哪些、commit 與否）。

### 例外

只有當使用者該次明確說「skip review」/「不用 review」/「直接 commit」時才跳過。沒說就一律跑。
