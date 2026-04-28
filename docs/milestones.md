# Milestones / Plan

- `/clear` 기능 추가. 이때, session id 가 달라지므로 이를 잘 핸들링. 그리고 idle 상태인 경우에만 수행하도록 ensure 해야 할 것 같다. 근데 모든 agent를 /clear 한 후, room close 했을때 codex한테는 "room_closed"가 가고, claude code한테는 안가는데. 왜지. 내가 생각한 바는 전부 안가야 하는데. 확인 필요. 실제로 session-id는 전부 clear후에 바뀜.

- user가 직접 /clear 했을때 핸들링 가능한지.. 즉 hook이 없다면 아쉬운대로 bridge_manage에 clear agent옵션을 추가하던가. 추가) hook은 존재함. sessionstart 에서 clear으로 감지 가능. 다만, sessionstart는 원래 resume도 감지 가능하다고 돼있는데 기존 구현 도중에서 resume을 제대로 감지 못했던 전과가 있음. 그때 이상하게 한건지 아니면 sessionstart에 버그가 있는건지 검토 필요.

- 코드 분할 작업.

- daemon 의 단일 `state_lock` 을 per-domain (queue / watchdog / alarm 등) 으로 분할하여 lock contention 줄이기. 코드 분할 작업과 함께 진행 가능.

- main loop 의 housekeeping (watchdog 검사, requeue, ingressing 처리 등) 을 별도 thread/timer 로 분리해서 command socket 처리 thread 의 lock 대기 시간 줄이기.

