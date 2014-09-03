# Copyright (c) 2014 Prashanth Raghu.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import collections
import time
import uuid

import mock
import redis

from zaqar.openstack.common.cache import cache as oslo_cache
from zaqar.openstack.common import timeutils
from zaqar.queues import storage
from zaqar.queues.storage.redis import controllers
from zaqar.queues.storage.redis import driver
from zaqar.queues.storage.redis import messages
from zaqar.queues.storage.redis import options
from zaqar.queues.storage.redis import utils
from zaqar import tests as testing
from zaqar.tests.queues.storage import base


def _create_sample_message(now=None, claimed=False, body=None):
    if now is None:
        now = timeutils.utcnow_ts()

    if claimed:
        claim_id = uuid.uuid4()
        claim_expires = now + 300
    else:
        claim_id = None
        claim_expires = now

    if body is None:
        body = {}

    return messages.Message(
        ttl=60,
        created=now,
        client_uuid=uuid.uuid4(),
        claim_id=claim_id,
        claim_expires=claim_expires,
        body=body
    )


class RedisUtilsTest(testing.TestBase):

    config_file = 'wsgi_redis.conf'

    def setUp(self):
        super(RedisUtilsTest, self).setUp()

        self.conf.register_opts(options.REDIS_OPTIONS,
                                group=options.REDIS_GROUP)

        self.redis_conf = self.conf[options.REDIS_GROUP]

        MockDriver = collections.namedtuple('MockDriver', 'redis_conf')

        self.driver = MockDriver(self.redis_conf)

    def test_scope_queue_name(self):
        self.assertEqual(utils.scope_queue_name('my-q'), '.my-q')
        self.assertEqual(utils.scope_queue_name('my-q', None), '.my-q')
        self.assertEqual(utils.scope_queue_name('my-q', '123'), '123.my-q')
        self.assertEqual(utils.scope_queue_name('my-q_1', '123'), '123.my-q_1')

        self.assertEqual(utils.scope_queue_name(), '.')
        self.assertEqual(utils.scope_queue_name(None, '123'), '123.')

    def test_scope_messages_set(self):
        self.assertEqual(utils.scope_message_ids_set('my-q'), '.my-q.')
        self.assertEqual(utils.scope_message_ids_set('my-q', 'p'), 'p.my-q.')
        self.assertEqual(utils.scope_message_ids_set('my-q', 'p', 's'),
                         'p.my-q.s')

        self.assertEqual(utils.scope_message_ids_set(None), '..')
        self.assertEqual(utils.scope_message_ids_set(None, '123'), '123..')
        self.assertEqual(utils.scope_message_ids_set(None, None, 's'), '..s')

    def test_descope_messages_set(self):
        key = utils.scope_message_ids_set('my-q')
        self.assertEqual(utils.descope_message_ids_set(key), ('my-q', None))

        key = utils.scope_message_ids_set('my-q', '123')
        self.assertEqual(utils.descope_message_ids_set(key), ('my-q', '123'))

        key = utils.scope_message_ids_set(None, '123')
        self.assertEqual(utils.descope_message_ids_set(key), (None, '123'))

        key = utils.scope_message_ids_set()
        self.assertEqual(utils.descope_message_ids_set(key), (None, None))

    def test_normalize_none_str(self):

        self.assertEqual(utils.normalize_none_str('my-q'), 'my-q')
        self.assertEqual(utils.normalize_none_str(None), '')

    def test_msg_claimed_filter(self):
        now = timeutils.utcnow_ts()

        unclaimed_msg = _create_sample_message()
        self.assertFalse(utils.msg_claimed_filter(unclaimed_msg, now))

        claimed_msg = _create_sample_message(claimed=True)
        self.assertTrue(utils.msg_claimed_filter(claimed_msg, now))

        # NOTE(kgriffs): Has a claim ID, but the claim is expired
        claimed_msg.claim_expires = now - 60
        self.assertFalse(utils.msg_claimed_filter(claimed_msg, now))

    def test_descope_queue_name(self):
        self.assertEqual(utils.descope_queue_name('p.q'), 'q')
        self.assertEqual(utils.descope_queue_name('.q'), 'q')
        self.assertEqual(utils.descope_queue_name('.'), '')

    def test_msg_echo_filter(self):
        msg = _create_sample_message()
        self.assertTrue(utils.msg_echo_filter(msg, msg.client_uuid))

        alt_uuid = utils.generate_uuid()
        self.assertFalse(utils.msg_echo_filter(msg, alt_uuid))

    def test_basic_message(self):
        now = timeutils.utcnow_ts()
        body = {
            'msg': 'Hello Earthlings!',
            'unicode': u'ab\u00e7',
            'bytes': b'ab\xc3\xa7',
            b'ab\xc3\xa7': 'one, two, three',
            u'ab\u00e7': 'one, two, three',
        }

        msg = _create_sample_message(now=now, body=body)
        basic_msg = msg.to_basic(now + 5)

        self.assertEqual(basic_msg['id'], msg.id)
        self.assertEqual(basic_msg['age'], 5)
        self.assertEqual(basic_msg['body'], body)
        self.assertEqual(basic_msg['ttl'], msg.ttl)

    def test_retries_on_connection_error(self):
        num_calls = [0]

        @utils.retries_on_connection_error
        def _raises_connection_error(self):
            num_calls[0] += 1
            raise redis.exceptions.ConnectionError

        self.assertRaises(redis.exceptions.ConnectionError,
                          _raises_connection_error, self)
        self.assertEqual(num_calls, [self.redis_conf.max_reconnect_attempts])


@testing.requires_redis
class RedisDriverTest(testing.TestBase):

    config_file = 'wsgi_redis.conf'

    def test_db_instance(self):
        cache = oslo_cache.get_cache()
        redis_driver = driver.DataDriver(self.conf, cache)

        self.assertTrue(isinstance(redis_driver.connection, redis.StrictRedis))


@testing.requires_redis
class RedisQueuesTest(base.QueueControllerTest):

    driver_class = driver.DataDriver
    config_file = 'wsgi_redis.conf'
    controller_class = controllers.QueueController

    def setUp(self):
        super(RedisQueuesTest, self).setUp()
        self.connection = self.driver.connection
        self.msg_controller = self.driver.message_controller

    def tearDown(self):
        super(RedisQueuesTest, self).tearDown()
        self.connection.flushdb()

    def test_inc_counter(self):
        queue_name = 'inc-counter'
        self.controller.create(queue_name)
        self.controller._inc_counter(queue_name, None, 10)

        scoped_q_name = utils.scope_queue_name(queue_name)
        count = self.controller._get_queue_info(scoped_q_name, b'c', int)[0]
        self.assertEqual(count, 10)

    def test_inc_claimed(self):
        self.addCleanup(self.controller.delete, 'test-queue',
                        project=self.project)

        queue_name = 'inc-claimed'

        self.controller.create(queue_name)
        self.controller._inc_claimed(queue_name, None, 10)

        scoped_q_name = utils.scope_queue_name(queue_name)
        claimed = self.controller._get_queue_info(scoped_q_name,
                                                  b'cl', int)[0]
        self.assertEqual(claimed, 10)


@testing.requires_redis
class RedisMessagesTest(base.MessageControllerTest):
    driver_class = driver.DataDriver
    config_file = 'wsgi_redis.conf'
    controller_class = controllers.MessageController

    def setUp(self):
        super(RedisMessagesTest, self).setUp()
        self.connection = self.driver.connection
        self.queue_ctrl = self.driver.queue_controller

    def tearDown(self):
        super(RedisMessagesTest, self).tearDown()
        self.connection.flushdb()

    def test_get_count(self):
        queue_name = 'get-count'
        self.queue_ctrl.create(queue_name)

        msgs = [{
            'ttl': 300,
            'body': 'di mo fy'
        } for i in range(0, 10)]

        client_id = uuid.uuid4()
        # Creating 10 messages
        self.controller.post(queue_name, msgs, client_id)

        messages_set_id = utils.scope_message_ids_set(queue_name, None,
                                                      'messages')

        num_msg = self.controller._get_count(messages_set_id)
        self.assertEqual(num_msg, 10)

    def test_empty_queue_exception(self):
        queue_name = 'empty-queue-test'
        self.queue_ctrl.create(queue_name)

        self.assertRaises(storage.errors.QueueIsEmpty,
                          self.controller.first, queue_name)

    def test_gc(self):
        self.queue_ctrl.create(self.queue_name)
        self.controller.post(self.queue_name,
                             [{'ttl': 0, 'body': {}}],
                             client_uuid=str(uuid.uuid4()))

        num_removed = self.controller.gc()
        self.assertEqual(num_removed, 1)

        for _ in range(100):
            self.controller.post(self.queue_name,
                                 [{'ttl': 0, 'body': {}}],
                                 client_uuid=str(uuid.uuid4()))

        num_removed = self.controller.gc()
        self.assertEqual(num_removed, 100)


@testing.requires_redis
class RedisClaimsTest(base.ClaimControllerTest):
    driver_class = driver.DataDriver
    config_file = 'wsgi_redis.conf'
    controller_class = controllers.ClaimController

    def setUp(self):
        super(RedisClaimsTest, self).setUp()
        self.connection = self.driver.connection
        self.queue_ctrl = self.driver.queue_controller
        self.message_ctrl = self.driver.message_controller

    def tearDown(self):
        super(RedisClaimsTest, self).tearDown()
        self.connection.flushdb()

    def test_claim_doesnt_exist(self):
        queue_name = 'no-such-claim'
        epoch = '000000000000000000000000'
        self.queue_ctrl.create(queue_name)
        self.assertRaises(storage.errors.ClaimDoesNotExist,
                          self.controller.get, queue_name,
                          epoch, project=None)

        claim_id, messages = self.controller.create(queue_name, {'ttl': 2,
                                                    'grace': 0},
                                                    project=None)

        # Lets let it expire
        time.sleep(2)
        self.assertRaises(storage.errors.ClaimDoesNotExist,
                          self.controller.update, queue_name,
                          claim_id, {}, project=None)

    def test_gc(self):
        self.queue_ctrl.create(self.queue_name)

        for _ in range(100):
            self.message_ctrl.post(self.queue_name,
                                   [{'ttl': 300, 'body': 'yo gabba'}],
                                   client_uuid=str(uuid.uuid4()))

        now = timeutils.utcnow_ts()
        timeutils_utcnow = 'zaqar.openstack.common.timeutils.utcnow_ts'

        # Test a single claim
        with mock.patch(timeutils_utcnow) as mock_utcnow:
            mock_utcnow.return_value = now - 1
            self.controller.create(self.queue_name, {'ttl': 1, 'grace': 60})

        num_removed = self.controller._gc(self.queue_name, None)
        self.assertEqual(num_removed, 1)

        # Test multiple claims
        with mock.patch(timeutils_utcnow) as mock_utcnow:
            mock_utcnow.return_value = now - 1

            for _ in range(5):
                self.controller.create(self.queue_name,
                                       {'ttl': 1, 'grace': 60})

        # NOTE(kgriffs): These ones should not be cleaned up
        self.controller.create(self.queue_name, {'ttl': 60, 'grace': 60})
        self.controller.create(self.queue_name, {'ttl': 60, 'grace': 60})

        num_removed = self.controller._gc(self.queue_name, None)
        self.assertEqual(num_removed, 5)