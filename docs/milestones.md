# Milestones / Plan

- 코드 분할 작업.

- daemon 의 단일 `state_lock` 을 per-domain (queue / watchdog / alarm 등) 으로 분할하여 lock contention 줄이기. 코드 분할 작업과 함께 진행 가능.

- main loop 의 housekeeping (watchdog 검사, requeue, ingressing 처리 등) 을 별도 thread/timer 로 분리해서 command socket 처리 thread 의 lock 대기 시간 줄이기.

- Reply가 11000자를 넘을 경우 truncate 되는것 file fallback으로 고치기.
