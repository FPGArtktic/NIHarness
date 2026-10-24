# sampler

The L2 fixture. It averages readings that fall inside a band.

Unlike the L1 fixture, this one is **green**: format, lint and tests all pass
on the baseline. That is the point. L2 measures review, and a reviewer may
only ACK when the build and the tests passed in its own run, so a fixture
with a deliberate failure in it would make every honest verdict a NAK.
