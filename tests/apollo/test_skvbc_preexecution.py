# Concord
#
# Copyright (c) 2020 VMware, Inc. All Rights Reserved.
#
# This product is licensed to you under the Apache 2.0 license (the "License").
# You may not use this product except in compliance with the Apache 2.0 License.
#
# This product may include a number of subcomponents with separate copyright
# notices and license terms. Your use of these subcomponents is subject to the
# terms and conditions of the subcomponent's license, as noted in the LICENSE
# file.

import os.path
import unittest
import trio
import random

from util.bft import with_trio, with_bft_network, KEY_FILE_PREFIX, with_constant_load
from util.skvbc_history_tracker import verify_linearizability
import util.bft_network_partitioning as net

SKVBC_INIT_GRACE_TIME = 2
NUM_OF_SEQ_WRITES = 100
NUM_OF_PARALLEL_WRITES = 1000
MAX_CONCURRENCY = 10
SHORT_REQ_TIMEOUT_MILLI = 3000
LONG_REQ_TIMEOUT_MILLI = 15000

def start_replica_cmd(builddir, replica_id):
    """
    Return a command that starts an skvbc replica when passed to
    subprocess.Popen.

    The replica is started with a short view change timeout and with RocksDB
    persistence enabled (-p).

    Note each arguments is an element in a list.
    """

    status_timer_milli = "500"
    view_change_timeout_milli = "10000"

    path = os.path.join(builddir, "tests", "simpleKVBC", "TesterReplica", "skvbc_replica")
    return [path,
            "-k", KEY_FILE_PREFIX,
            "-i", str(replica_id),
            "-s", status_timer_milli,
            "-v", view_change_timeout_milli,
            "-p",
            "-t", os.environ.get('STORAGE_TYPE')
            ]


class SkvbcPreExecutionTest(unittest.TestCase):

    __test__ = False  # so that PyTest ignores this test scenario

    async def send_single_read(self, skvbc, client):
        req = skvbc.read_req(skvbc.random_keys(1))
        await client.read(req)

    async def send_single_write_with_pre_execution_and_kv(self, skvbc, write_set, client, long_exec=False):
        reply = await client.write(skvbc.write_req([], write_set, 0, long_exec), pre_process=True)
        reply = skvbc.parse_reply(reply)
        self.assertTrue(reply.success)

    async def send_single_write_with_pre_execution(self, skvbc, client, long_exec=False):
        write_set = [(skvbc.random_key(), skvbc.random_value()),
                     (skvbc.random_key(), skvbc.random_value())]
        await self.send_single_write_with_pre_execution_and_kv(skvbc, write_set, client, long_exec)

    async def run_concurrent_pre_execution_requests(self, skvbc, clients, num_of_requests, write_weight=.90):
        sent = 0
        write_count = 0
        read_count = 0
        while sent < num_of_requests:
            async with trio.open_nursery() as nursery:
                for client in clients:
                    if random.random() <= write_weight:
                        nursery.start_soon(self.send_single_write_with_pre_execution, skvbc, client)
                        write_count += 1
                    else:
                        nursery.start_soon(self.send_single_read, skvbc, client)
                        read_count += 1
                    sent += 1
                    if sent == num_of_requests:
                        break
            await trio.sleep(.1)
        return read_count + write_count

    @with_trio
    @with_bft_network(start_replica_cmd, selected_configs=lambda n, f, c: n == 7)
    @with_constant_load
    async def test_long_request_with_constant_load(self, bft_network, skvbc, nursery):
        """
        In this test we make sure a long-running request executes
        concurrently with a constant system load in the background.
        """
        bft_network.start_all_replicas()

        write_set = [(skvbc.random_key(), skvbc.random_value()),
                     (skvbc.random_key(), skvbc.random_value())]

        client = bft_network.random_client()
        client.config = client.config._replace(
            req_timeout_milli=LONG_REQ_TIMEOUT_MILLI,
            retry_timeout_milli=1000
        )

        await self.send_single_write_with_pre_execution_and_kv(
            skvbc, write_set, client, long_exec=True)

        # Wait for some background "constant load" requests to execute
        await trio.sleep(seconds=5)

        # Let's just check no view change occurred in the meantime
        initial_primary = 0
        await bft_network.wait_for_view(replica_id=initial_primary,
                                        expected=lambda v: v == initial_primary,
                                        err_msg="Make sure the view did not change.")

    @with_trio
    @with_bft_network(start_replica_cmd)
    @with_constant_load
    async def test_pre_execution_with_added_constant_load(self, bft_network, skvbc, nursery):
        """
        Run a batch of concurrent pre-execution requests, while
        sending a constant "time service like" load on the normal execution path.

        This test validates that pre-execution and normal execution coexist correctly.
        """
        bft_network.start_all_replicas()
        num_preexecution_requests = 200

        clients = bft_network.random_clients(MAX_CONCURRENCY)
        await self.run_concurrent_pre_execution_requests(
            skvbc, clients, num_preexecution_requests, write_weight=1)


        client = bft_network.random_client()
        current_block = skvbc.parse_reply(
            await client.read(skvbc.get_last_block_req()))

        self.assertTrue(current_block > num_preexecution_requests,
                        "Make sure all pre-execution requests were processed, in"
                        "addition to the constant load in the background.")
