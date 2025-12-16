# Project progress

- [x] Phase 1 - Project setup and TCP socket creation
  - [x] Strict GCC build flags and reproducible Makefile
  - [x] `socket(AF_INET, SOCK_STREAM, 0)` and descriptor cleanup
  - [x] Initial socket lifecycle smoke test
- [x] Phase 2 - Bind, listen, and accept a client
  - [x] Bind an IPv4 TCP listener on `127.0.0.1` (default port `9090`)
  - [x] Support port `0` for OS-selected ephemeral ports
  - [x] Enter `LISTEN` state and accept one TCP client
  - [x] Report connecting client's address and close both descriptors
  - [x] Validate CLI port input, occupied port errors, and normal operation
  - [x] Automated real-socket tests via `make test`
- [ ] Phase 3 - TCP echo and backend client
