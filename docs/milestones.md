# Milestones / Plan

- daemon 의 책임 영역과 state ownership 을 먼저 분리한 뒤, per-target/per-domain (queue / watchdog / alarm 등) lock 으로 단계적으로 분할하여 lock contention 줄이기. 완료: `bridge_daemon.py` 는 facade 로 남기고 command/status/watchdog/delivery/event/aggregate/interrupt/clear/maintenance/store/state/tmux/message 서비스를 별도 모듈로 분리했다.

- main loop 의 housekeeping (watchdog 검사, requeue, ingressing 처리 등) 을 별도 thread/timer 로 분리해서 command socket 처리 thread 의 lock 대기 시간 줄이기. 완료: `bridge_daemon_maintenance.py` 의 scheduler 가 watchdog, stale inflight, ingressing promotion, retry-enter, capture cleanup, delivery tick 을 주기적으로 실행한다.

- command socket latency 줄이기. 완료: unrelated-target delivery/interrupt/controlled-clear tmux IO 는 `state_lock` 밖에서 target lock 또는 clear reservation gate 로 보호하고, watchdog/alarm state 는 `watchdog_lock` 으로 분리했다.

- 최종 lock order 문서화. 현재 routing order 는 target lock(s) sorted by alias -> `state_lock` -> `watchdog_lock` -> `QueueStore.update()` file lock 이며, `AggregateStore` 는 이 체인 밖에서 별도 phase 로 읽고 쓴다. `state_lock` 은 participant/session/pane cache 와 짧은 routing cache mutation 을 위한 deliberate coarse region 으로 남는다.

- 남은 deliberate limits 는 `docs/known-issues.md` 에서 운영 관점으로 추적한다: live routing state 는 여전히 daemon memory 이고, daemon restart 는 일부 watchdog/alarm context 를 복구하지 못한다.

- Detailed staged plan: `docs/daemon-refactor-plan.md`.
