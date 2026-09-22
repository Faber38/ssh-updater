# SSH transport limit (R1)

AsyncSSH 2.24.0 from PyPI has no public receive-packet limit at the SSH
transport layer. `window` and `max_pktsize` constrain channels, not the initial
transport packet header. Application output caps and connection timeouts do
not fix this: an unauthenticated peer can advertise a huge packet and feed it
before the timeout. An external TCP filter cannot generally inspect encrypted
packet lengths.

The approved solution uses the unmodified official upstream commit
[459f44515238880b7be68299bb7d1fc0e704121d](https://github.com/ronf/asyncssh/commit/459f44515238880b7be68299bb7d1fc0e704121d).
It checks the decoded packet length immediately after receiving its header,
including before authentication. The initial maximum is 256 KiB. A locally
requested channel `max_pktsize` can increase it; this application uses the
standard 32 KiB channel packet size. Direct connections and manually opened
ProxyJump connections use the same library implementation.

The source archive URL pins the full commit and SHA256
`653151d93fc2b62d402dc696f53be3f76541847b0da0daf4212443d956c04674`.
No monkey patch or private AsyncSSH API is used to implement this limit. This is
an **unreleased upstream revision**, still reporting version `2.24.0`, not the
PyPI release with that version. `scripts/release.py environment` additionally
checks installed `direct_url.json` provenance and archive hash. Both release
jobs inherit the same pin through requirements-build.txt.

The transport regression uses a real TCP peer which sends a fragmented SSH
header advertising 256 KiB + 1 or nearly 4 GiB, without sending the payload.
The application must raise ProtocolError within one second with traced Python
allocations below 2 MiB. The unpatched library must fail this test by timing
out. Normal encrypted SSH sessions, host-key checks and ProxyJump remain
covered by the integration suite. This is a packet-framing defense, not a
claim that all hostile-server CPU/memory denial-of-service cases are solved.

Replace the source pin with an official release containing this fix after
repeating transport, SSH and release compatibility tests. Do not replace it
with PyPI `asyncssh==2.24.0` based on the reported version alone.

Sources checked 2026-09-22:
- https://asyncssh.readthedocs.io/en/latest/api.html
- https://pypi.org/project/asyncssh/ (latest published release: 2.24.0)
- Upstream commit linked above, including its channel regression tests.
