from __future__ import absolute_import

import struct
from collections import namedtuple

import mock
import pytest
from kafka.consumer.fetcher import ConsumerRecord
from kafka.structs import TopicPartition
from six.moves import range

from kafka_utils.kafka_consumer_manager.util import get_group_partition
from kafka_utils.kafka_consumer_manager.util import get_offset_topic_partition_count
from kafka_utils.kafka_consumer_manager.util import InvalidMessageException
from kafka_utils.kafka_consumer_manager.util import KafkaGroupReader
from kafka_utils.util.error import UnknownTopic
from kafka_utils.util.offsets import PartitionOffsets

Message = namedtuple("Message", ["partition", "offset", "key", "value"])


class TestKafkaGroupReader(object):

    groups = ['^test\..*', '^my_test$', '^my_test2$']

    key_ok = b''.join([
        struct.pack('>h', 0),  # Schema: offset commit
        struct.pack('>h6s', 6, b'group1'),  # Group name
        struct.pack('>h6s', 6, b'topic1'),  # Topic name
        struct.pack('>l', 15),  # Partition
    ])

    value_ok = b''.join([
        struct.pack('>h', 0),  # Schema: version 0
        struct.pack('>q', 123),  # Offset 123
    ])

    key_wrong = b''.join([
        struct.pack('>h', 2),  # Schema: group message
        struct.pack('>h6s', 6, b'group1'),  # Group name
        struct.pack('>h6s', 6, b'topic1'),  # Topic name
        struct.pack('>l', 15),  # Partition
    ])

    value_wrong = b''.join([
        struct.pack('>h', 3),  # Schema: invalid
        struct.pack('>q', 123),  # Offset 123
    ])

    def test_parse_consumer_offset_message_correct(self):
        kafka_config = mock.Mock()
        kafka_group_reader = KafkaGroupReader(kafka_config)
        message = Message(0, '__consumer_offsets', self.key_ok, self.value_ok)
        group, topic, partition, offset = kafka_group_reader.parse_consumer_offset_message(message)

        assert group == 'group1'
        assert topic == 'topic1'
        assert partition == 15
        assert offset == 123

    def test_parse_consumer_offset_message_no_value(self):
        kafka_config = mock.Mock()
        kafka_group_reader = KafkaGroupReader(kafka_config)
        message = Message(0, '__consumer_offsets', self.key_ok, None)
        group, topic, partition, offset = kafka_group_reader.parse_consumer_offset_message(message)

        assert group == 'group1'
        assert topic == 'topic1'
        assert partition == 15
        assert offset is None

    @mock.patch.object(KafkaGroupReader, 'parse_consumer_offset_message')
    def test_process_consumer_offset_message_group(self, parse_mock):
        parse_mock.side_effect = [('test.a', 'topic1', 0, 123),
                                  ('test.a', 'topic1', 1, 124),
                                  ('test.a', 'topic2', 0, 125),
                                  ('my_test', 'topic1', 0, 123),
                                  ('my_test', 'topic2', 0, 124),
                                  ('my_test', 'topic2', 0, None),
                                  ('my_test2', 'topic3', 0, 123), ]
        kafka_group_reader = KafkaGroupReader(mock.Mock())
        for _ in range(7):
            message = mock.MagicMock(spec=ConsumerRecord)
            kafka_group_reader.process_consumer_offset_message(message)

        expected = {'test.a': {'topic1', 'topic2'}, 'my_test2': {'topic3'}, 'my_test': {'topic1'}}
        assert kafka_group_reader.kafka_groups == expected

    @mock.patch.object(KafkaGroupReader, 'parse_consumer_offset_message')
    def test_process_consumer_offset_message_invalid_message(self, parse_mock):
        parse_mock.side_effect = InvalidMessageException
        kafka_group_reader = KafkaGroupReader(mock.Mock())
        message = mock.MagicMock(spec=ConsumerRecord)
        kafka_group_reader.process_consumer_offset_message(message)

        assert kafka_group_reader.kafka_groups == dict()

    def test_process_consumer_offset_message_topic_get(self):
        kafka_config = mock.Mock()
        kafka_group_reader = KafkaGroupReader(kafka_config)

        assert kafka_group_reader.kafka_groups == {}

        with mock.patch.object(
            kafka_group_reader,
            'parse_consumer_offset_message',
            return_value=[
                'test_group',
                'test_topic',
                0,
                45
            ],
            autospec=True
        ):
            kafka_group_reader.process_consumer_offset_message('test message')
            assert kafka_group_reader.kafka_groups['test_group'] == {'test_topic'}

    def test_process_consumer_offset_message_topic_pop_no_offset(self):
        kafka_config = mock.Mock()
        kafka_group_reader = KafkaGroupReader(kafka_config)

        kafka_group_reader.kafka_groups['test_group'] = set(['test_topic'])
        assert kafka_group_reader.kafka_groups['test_group'] == set(['test_topic'])

        with mock.patch.object(
            kafka_group_reader,
            'parse_consumer_offset_message',
            return_value=[
                'test_group',
                'test_topic',
                0,
                None
            ],
            autospec=True
        ):
            kafka_group_reader.process_consumer_offset_message('test message')
            assert kafka_group_reader.kafka_groups == {'test_group': set([])}

    def test_read_groups(self):
        kafka_config = mock.Mock()
        kafka_group_reader = KafkaGroupReader(kafka_config)
        with mock.patch(
            'kafka_utils.kafka_consumer_manager.util.KafkaConsumer',
        ) as mock_consumer:
            with mock.patch.object(
                kafka_group_reader,
                'get_current_watermarks',
                return_value={
                    0: PartitionOffsets('__consumer_offsets', 0, 45, 0),
                    1: PartitionOffsets('__consumer_offsets', 1, 20, 0),
                    2: PartitionOffsets('__consumer_offsets', 2, 25, 25),
                    3: PartitionOffsets('__consumer_offsets', 3, 0, 0),
                },
                autospec=True
            ):
                with mock.patch.object(
                    kafka_group_reader,
                    'parse_consumer_offset_message',
                    side_effect=iter([
                        ('test_group', 'test_topic', 0, 45),
                        ('test_group2', 'test_topic2', 0, 20),
                    ]),
                    autospec=True,
                ):
                    mock_consumer.return_value.__iter__.return_value = iter([
                        mock.Mock(offset=44, partition=0, topic='test_topic'),
                        mock.Mock(offset=19, partition=1, topic='test_topic'),
                    ])
                    mock_consumer.return_value.partitions_for_topic.return_value = [0, 1]
                    kafka_group_reader.read_groups()
                    assert kafka_group_reader.kafka_groups['test_group'] == {"test_topic"}
                    assert kafka_group_reader.kafka_groups['test_group2'] == {"test_topic2"}
                    mock_consumer.return_value.assign.call_args_list == [
                        mock.call([
                            TopicPartition("__consumer_offsets", 0),
                            TopicPartition("__consumer_offsets", 1),
                        ]),
                        mock.call([TopicPartition("__consumer_offsets", 0)]),
                    ]

    def test_read_groups_with_partition(self):
        kafka_config = mock.Mock()
        kafka_group_reader = KafkaGroupReader(kafka_config)
        with mock.patch(
            'kafka_utils.kafka_consumer_manager.util.KafkaConsumer',
        ) as mock_consumer:
            with mock.patch.object(
                kafka_group_reader,
                'get_current_watermarks',
                return_value={
                    0: PartitionOffsets(
                        'test_topic',
                        0,
                        45,
                        0
                    )
                },
                autospec=True
            ):
                with mock.patch.object(
                    kafka_group_reader,
                    'parse_consumer_offset_message',
                    return_value=[
                        'test_group',
                        'test_topic',
                        0,
                        45
                    ],
                    autospec=True
                ):
                    mock_consumer.return_value.__iter__.return_value = iter([mock.Mock(partition=0, topic='test_topic', offset=45)])
                    kafka_group_reader.read_groups(partition=0)
                    assert kafka_group_reader.kafka_groups['test_group'] == {"test_topic"}
                    mock_consumer.return_value.assign.assert_called_once_with(
                        [TopicPartition("__consumer_offsets", 0)]
                    )

    @mock.patch("kafka_utils.kafka_consumer_manager.util.get_topic_partition_metadata")
    def test_get_offset_topic_partition_count_raise(self, mock_get_metadata):
        mock_get_metadata.return_value = {'topic1': {0: None}}
        kafka_config = mock.Mock(broker_list=['localhost:9092'])
        with pytest.raises(UnknownTopic):
            get_offset_topic_partition_count(kafka_config)

    @mock.patch("kafka_utils.kafka_consumer_manager.util.get_topic_partition_metadata")
    def test_get_offset_topic_partition_count(self, mock_get_metadata):
        mock_get_metadata.return_value = {'topic1': {0: None},
                                          '__consumer_offsets': {0: None, 1: None}}
        kafka_config = mock.Mock(broker_list=['localhost:9092'])
        assert get_offset_topic_partition_count(kafka_config) == 2

    def test_get_group_partition(self):
        result1 = get_group_partition('815e79b2-be20-11e6-96b6-0697c842cbe5', 50)
        result2 = get_group_partition('83e3f292-be26-11e6-b509-0697c842cbe5', 50)
        result3 = get_group_partition('adaceffc-be26-11e6-8eab-0697c842cbe5', 20)

        assert result1 == 10
        assert result2 == 44
        assert result3 == 5
