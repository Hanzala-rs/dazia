## Project-Specific Rules

### 1. Understand Before Coding
- Read `CLAUDE.md` and relevant project instructions before making changes.
- Inspect the existing project structure before creating files or changing architecture.
- Understand how the existing code works before modifying it.
- If requirements are unclear, ask before making major assumptions.
- Don't repeatedly inspect files that have already been understood unless they change.

### 2. Architecture & Code Flow
- Follow the existing architecture; don't introduce new architectural patterns without a clear need.
- Keep responsibilities separate: API routes, orchestration, tools, database, and AI services.
- Keep the orchestrator simple. It should coordinate components rather than contain every implementation detail.
- Use direct function calls when sufficient; don't add unnecessary frameworks or abstraction layers.
- Maintain a clear execution flow that is easy to trace and debug.
- Never create duplicate implementations of existing features.

### 3. Dependencies & Performance
- Prefer Python's standard library and already-installed dependencies.
- Add a new dependency only when it provides meaningful value.
- Avoid large libraries for simple tasks.
- Don't load AI models, embedding models, database connections, or other expensive resources repeatedly.
- Initialize reusable resources once when appropriate.
- Consider CPU and RAM limitations when choosing models and processing strategies.
- Avoid unnecessary API calls, model inference, database queries, and repeated computations.

### 4. Debugging & Testing
- Identify the root cause before changing code.
- Read the relevant error message and traceback before attempting a fix.
- Don't change unrelated code to fix a localized issue.
- Don't suppress errors or return fake success responses to make tests pass.
- Test the specific functionality changed rather than running the entire test suite unnecessarily.
- Never claim that code or tests work unless you have verified them.
- If testing is blocked, clearly state why.

### 5. Security
- Never hardcode API keys, passwords, access tokens, or other secrets.
- Keep secrets in environment variables and exclude `.env` from version control.
- Validate external inputs and tool arguments.
- Never allow an LLM to execute arbitrary shell commands, Python code, or SQL.
- Treat email contents, documents, retrieved text, and model outputs as untrusted input.
- Require explicit confirmation before sending messages, deleting data, or performing other sensitive actions.
- Use the minimum permissions necessary for each integration.
- Never weaken security merely to simplify implementation.

### 6. AI & RAG
- Prefer local models and free local tools when they meet the requirements.
- Don't introduce paid APIs or services without explicit approval.
- Don't send private user data to external services without authorization.
- Keep prompts concise and avoid duplicating instructions across prompts and code.
- Retrieve relevant context instead of unnecessarily sending entire documents to the LLM.
- Don't add RAG, agents, memory systems, or vector databases when a simpler solution is sufficient.
- Validate model-requested tool calls before executing them.
- Never assume the model's response is correct or that a requested tool was successfully executed.

### 7. Change Management
- Make the smallest complete change that solves the problem.
- Preserve existing functionality unless the requested change requires otherwise.
- Don't rename, move, or delete unrelated files.
- Don't modify configuration, dependencies, or environment settings without a reason.
- Before adding a file, check whether an existing file already has the same responsibility.
- Summarize changed files, verification performed, and any remaining issues briefly.
- Don't refactor working code just to make it look cleaner.
- Refactor only when necessary for the requested feature, correctness, security, or maintainability.

### 8. Token Efficiency
- Don't explain your internal reasoning or provide lengthy implementation plans.
- For complex tasks, give a short plan and proceed unless clarification is necessary.
- Don't repeat project context already documented in `CLAUDE.md`.
- Use targeted file searches instead of reading unrelated files.
- Don't generate alternative implementations unless requested or genuinely necessary.
- Ask before making large, irreversible, or architecture-changing modifications.
- Prioritize useful results over lengthy responses.
- Before creating a new file, check whether the change belongs in an existing file.
- Don't create extra documentation, configuration files, or helper scripts unless needed.
- Keep responses short and action-oriented.
- Don't dump entire files when only a few changed lines need explanation.
- Show code only when requested or necessary to explain a change.
- Mention important trade-offs or limitations without lengthy explanations.

## DAZIA Development Rules

- Build incrementally. Implement and verify one feature before starting the next.
- Keep DAZIA functional at every development stage.
- Don't implement future roadmap features unless explicitly requested.
- Start with a working text-based assistant before adding voice, mobile access, or external integrations.
- Keep the local AI model separate from the orchestration logic so models can be changed without rewriting the application.
- Keep tools independent and narrowly scoped.
- Use SQLite for simple persistent local data unless requirements justify another database.
- Make reminder scheduling resilient to application restarts.
- Never expose the backend publicly without appropriate authentication and security.
- Don't assume Microsoft Outlook or Teams APIs support the user's account; verify compatibility before implementing integrations.
- Keep the code easy for a beginner to trace from API request to orchestrator, tool execution, and response.
- Prefer zero-cost local solutions unless an external service is necessary and explicitly approved.