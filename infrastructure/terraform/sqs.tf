# -- Async worker queue --------------------------------------------------------
# The API Lambda validates an upload, writes the job row, and pushes a message
# here, then returns in well under a second. The Worker Lambda (lambda.tf) drains
# the queue and runs the heavy OCR / multi-agent pipeline. This decouples the
# thin request path from the heavy batch path and gives us three things
# self-invocation never did: a visibility timeout that stops a still-running job
# being redelivered, automatic retry on transient failure, and a queue-depth
# metric to alarm on for backpressure.

resource "aws_sqs_queue" "worker" {
  name = "${local.name_prefix}-worker"

  # Must exceed the Worker Lambda timeout (and the ~130s orchestrator ceiling) so
  # a job still running is never handed to a second consumer. Kept a comfortable
  # margin above var.worker_lambda_timeout_seconds.
  visibility_timeout_seconds = var.worker_queue_visibility_timeout_seconds

  # Long-poll to cut empty receives (cost + noise).
  receive_wait_time_seconds = 20

  message_retention_seconds = 86400 # 1 day - jobs are short-lived; stale is useless

  # After maxReceiveCount failed deliveries the message is a poison pill: route it
  # to the DLQ where it becomes a visible, alertable event instead of churning.
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.worker_dlq.arn
    maxReceiveCount     = var.worker_queue_max_receive_count
  })

  tags = local.common_tags
}

# Dead-letter queue - terminal home for messages that fail past maxReceiveCount.
# Retained the full 14 days so a poison message can be inspected / replayed.
resource "aws_sqs_queue" "worker_dlq" {
  name                      = "${local.name_prefix}-worker-dlq"
  message_retention_seconds = 1209600 # 14 days (max)
  tags                      = local.common_tags
}

# Only the main worker queue may redrive into the DLQ.
resource "aws_sqs_queue_redrive_allow_policy" "worker_dlq" {
  queue_url = aws_sqs_queue.worker_dlq.id
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.worker.arn]
  })
}

# -- Queue -> Worker Lambda ----------------------------------------------------
# batch_size defaults to 1: each resume is a heavy, long-running job, so one
# message == one invocation lets Lambda scale a batch burst elastically instead
# of serialising files under a single timeout. ReportBatchItemFailures lets the
# handler ack the successes and redeliver only the failures.

resource "aws_lambda_event_source_mapping" "worker" {
  event_source_arn                   = aws_sqs_queue.worker.arn
  function_name                      = aws_lambda_function.worker.arn
  batch_size                         = var.worker_sqs_batch_size
  maximum_batching_window_in_seconds = 0
  function_response_types            = ["ReportBatchItemFailures"]

  # Optional ceiling on concurrent worker invocations pulled off this queue -
  # the backpressure lever that protects the OpenAI TPM budget during a big batch
  # burst. Omitted (unbounded, up to account concurrency) when the var is 0.
  dynamic "scaling_config" {
    for_each = var.worker_event_source_max_concurrency > 0 ? [1] : []
    content {
      maximum_concurrency = var.worker_event_source_max_concurrency
    }
  }
}

# -- Alarms --------------------------------------------------------------------
# All alarms fire to var.alarm_sns_topic_arn when set; otherwise they still show
# in the CloudWatch console and can be wired to notifications later.

locals {
  alarm_actions = var.alarm_sns_topic_arn == "" ? [] : [var.alarm_sns_topic_arn]
}

# A poison message reached the DLQ - the single most important signal: a job that
# will never finish on its own and needs a human.
resource "aws_cloudwatch_metric_alarm" "worker_dlq_not_empty" {
  alarm_name          = "${local.name_prefix}-worker-dlq-not-empty"
  alarm_description   = "A parse job failed past all retries and landed in the DLQ."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = { QueueName = aws_sqs_queue.worker_dlq.name }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
  tags          = local.common_tags
}

# Backlog building up on the main queue - the backpressure signal that was
# invisible under self-invocation.
resource "aws_cloudwatch_metric_alarm" "worker_queue_backlog" {
  alarm_name          = "${local.name_prefix}-worker-queue-backlog"
  alarm_description   = "Worker queue backlog is high - jobs are arriving faster than they drain."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 5
  threshold           = var.worker_queue_backlog_alarm_threshold
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = { QueueName = aws_sqs_queue.worker.name }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
  tags          = local.common_tags
}

# Worker Lambda throwing - covers OOM/timeouts and unexpected faults that will
# drive redeliveries (and eventually DLQ arrivals).
resource "aws_cloudwatch_metric_alarm" "worker_errors" {
  alarm_name          = "${local.name_prefix}-worker-errors"
  alarm_description   = "Worker Lambda is erroring - jobs are failing to process."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = var.worker_errors_alarm_threshold
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = { FunctionName = aws_lambda_function.worker.function_name }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
  tags          = local.common_tags
}
