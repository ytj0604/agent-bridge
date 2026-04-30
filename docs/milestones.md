# Milestones / Plan

- daemon 의 책임 영역과 state ownership 을 먼저 분리한 뒤, per-target/per-domain (queue / watchdog / alarm 등) lock 으로 단계적으로 분할하여 lock contention 줄이기.

- main loop 의 housekeeping (watchdog 검사, requeue, ingressing 처리 등) 을 별도 thread/timer 로 분리해서 command socket 처리 thread 의 lock 대기 시간 줄이기.

- Detailed staged plan: `docs/daemon-refactor-plan.md`.
