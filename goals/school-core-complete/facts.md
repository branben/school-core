# Facts

- A configured issue for a supported repository can complete the school-core flow through a reviewable pull request targeting that same repository; school-core does not merge it automatically.
- A second supported repository can be added through documented configuration and complete the same task flow without a code fork, using that repository's own clone, base identity, toolchain, and verification policy.
- Student coding tasks run with the agent, repository clone, and shell inside a disposable VM; host credentials and host files are not exposed, model access is mediated, and VM/runtime failures never fall back to host code execution.
- A separate trusted verifier checks the exact returned candidate against school-controlled checks; repository-defined checks are reported separately as untrusted extra evidence, and all results are bound to the task, base commit, and candidate identity.
- Teacher review receives the candidate, changed files, trusted verification evidence, and student report; approval, rejection, and requested changes persist across restart, and PR creation follows the configured human-approval policy.
- Completed tasks record grounded outcomes, teacher feedback, and tool/skill-use evidence; per-skill and overall growth measures persist and can inform later task routing.
- Useful task learning is consolidated and retrievable in later cycles with repository context kept correctly scoped; retention rules bound historical board, run, and evidence data without losing required audit records.
- A controlled hosted pilot can recover durable task, review, and evidence state after restart; interrupted, timed-out, or dead-letter tasks become visible and recoverable without duplicate PRs or false passes.
- The hosted pilot has documented setup and recovery, authenticated operator/teacher access, observable task lifecycle and failure states, and resource limits for student execution.
- A clean-environment acceptance rehearsal demonstrates issue intake through cloned-repo task, isolated student execution, trusted verification, teacher decision, reviewable PR, durable learning/evidence, and successful teardown; injected failures prove fail-closed behavior and cleanup.
