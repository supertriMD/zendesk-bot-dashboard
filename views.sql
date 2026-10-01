-- Reporting views over the zendesk_bot tables. Applied by db.init_db() on every pull
-- (CREATE OR REPLACE, so this file is the single definition). ${DS} = project.dataset.
-- Statements are separated by a line holding only ";;".
--
-- v_questions — ONE ROW PER QUESTION THAT CAME IN, every channel:
--   source = 'ticket'    every Zendesk ticket (email, web form, web messaging), with our LLM
--                        verdict + topic, Zendesk's own tags/fields, response speed, and the
--                        bot conversation behind it when there is one.
--   source = 'bot_only'  a bot conversation (AI agents export) that never became a ticket. No
--                        text exists for these; topic comes from the help-centre articles the
--                        bot answered from and the signals it captured.
-- Link between the two: an email-bot conversation's platform id IS the ticket id; a messaging
-- conversation's id is recorded in the ticket's audits (ticket_attributes.messaging_conversation_id).
-- Grain is conversations, not people. No contact details are in any column.

CREATE OR REPLACE VIEW `${DS}.v_questions` AS
WITH unioned AS (
WITH bot AS (
  SELECT
    b.*,
    ARRAY(SELECT JSON_VALUE(k, '$.article_name') FROM UNNEST(JSON_QUERY_ARRAY(b.knowledge_sources_json)) k) AS bot_articles,
    JSON_VALUE(b.session_signals_json, '$.event_name') AS bot_event_name,
    SAFE_CAST(JSON_VALUE(b.session_signals_json, '$.first_timer_or_fear') AS BOOL) AS bot_first_timer_signal,
    SAFE_CAST(JSON_VALUE(b.session_signals_json, '$.wants_escalation') AS BOOL) AS bot_wants_escalation
  FROM `${DS}.bot_conversations` b
  WHERE NOT IFNULL(b.test_mode, FALSE)
  -- one bot conversation per link key (latest wins), so a ticket never fans out
  QUALIFY ROW_NUMBER() OVER (PARTITION BY b.platform_conversation_id
                             ORDER BY b.conversation_end_time DESC, b.pulled_at DESC) = 1
),
tickets AS (
  SELECT
    c.conversation_id,
    IF(c.channel = 'messaging', ta.messaging_conversation_id, c.conversation_id) AS bot_link_key,
    c.channel, ta.via_channel, c.created_at, c.status, c.subject,
    c.bot_participated, c.ended_with_human, c.turn_count,
    s.resolution AS our_resolution, s.primary_topic, s.unanswered_reason,
    rp.resolution_path,
    ta.zd_resolution_tier, ta.zd_resolution_type, ta.zd_event, ta.zd_race_division, ta.zd_topic,
    ta.tags, ta.satisfaction_score,
    m.reply_time_min, m.first_resolution_time_min, m.full_resolution_time_min,
    m.requester_wait_time_min, m.reopens, m.replies
  FROM `${DS}.conversations` c
  LEFT JOIN `${DS}.scores` s USING (conversation_id)
  LEFT JOIN `${DS}.resolution_paths` rp USING (conversation_id)
  LEFT JOIN `${DS}.ticket_attributes` ta USING (conversation_id)
  LEFT JOIN `${DS}.ticket_metrics` m USING (conversation_id)
)
SELECT
  'ticket' AS source,
  t.conversation_id, t.channel, t.via_channel, t.created_at, DATE(t.created_at) AS question_date,
  COALESCE(t.zd_event, b.bot_event_name,
           (SELECT tag FROM UNNEST(t.tags) tag
            WHERE REGEXP_CONTAINS(tag, r'^ai_(chicago|toronto|kerrville|blenheim|long_beach|new_jersey|austin|toulouse|brighton|chantilly|new_york)$')
            LIMIT 1)) AS event_raw,
  t.subject, t.status, t.turn_count,
  t.bot_participated OR b.conversation_id IS NOT NULL AS bot_involved,
  t.ended_with_human,
  t.our_resolution, t.primary_topic, t.unanswered_reason, t.resolution_path,
  COALESCE(b.resolution_tier, t.zd_resolution_tier) AS zendesk_resolution_tier,
  b.automated_resolution AS zendesk_automated_resolution,
  b.conversation_status AS bot_status, b.conversation_type AS bot_conversation_type,
  -- knowledge_not_understood_count is the counter that actually fires (~1 in 5 bot chats);
  -- not_understood_messages_count and knowledge_fallback_count read 0 on every row (30 Sep 2026).
  b.knowledge_not_understood_count AS bot_not_understood, b.knowledge_fallback_count AS bot_fallbacks,
  b.bot_articles, b.bot_first_timer_signal, b.bot_wants_escalation, b.language AS bot_language,
  t.zd_race_division, t.zd_topic, t.tags, t.satisfaction_score,
  t.reply_time_min, t.first_resolution_time_min, t.full_resolution_time_min,
  t.requester_wait_time_min, t.reopens, t.replies,
  b.conversation_id AS bot_conversation_id
FROM tickets t
LEFT JOIN bot b ON b.platform_conversation_id = t.bot_link_key

UNION ALL

SELECT
  'bot_only' AS source,
  b.conversation_id, 'messaging' AS channel, b.channel AS via_channel,
  b.conversation_start_time AS created_at, DATE(b.conversation_start_time) AS question_date,
  COALESCE(b.bot_event_name,
           (SELECT a FROM UNNEST(b.bot_articles) a
            WHERE REGEXP_CONTAINS(LOWER(a), r'chicago|toronto|kerrville|blenheim|long beach|new jersey|austin|toulouse|brighton|chantilly')
            LIMIT 1)) AS event_raw,   -- else the event named by the article the bot answered from
  CAST(NULL AS STRING) AS subject, CAST(NULL AS STRING) AS status,
  b.visitor_messages_count + b.bot_messages_count AS turn_count,
  TRUE AS bot_involved, FALSE AS ended_with_human,
  CAST(NULL AS STRING) AS our_resolution, CAST(NULL AS STRING) AS primary_topic,
  CAST(NULL AS STRING) AS unanswered_reason, CAST(NULL AS STRING) AS resolution_path,
  b.resolution_tier AS zendesk_resolution_tier, b.automated_resolution AS zendesk_automated_resolution,
  b.conversation_status AS bot_status, b.conversation_type AS bot_conversation_type,
  b.knowledge_not_understood_count, b.knowledge_fallback_count,
  b.bot_articles, b.bot_first_timer_signal, b.bot_wants_escalation, b.language,
  CAST(NULL AS STRING), CAST(NULL AS STRING), CAST([] AS ARRAY<STRING>), CAST(NULL AS STRING),
  CAST(NULL AS INT64), CAST(NULL AS INT64), CAST(NULL AS INT64), CAST(NULL AS INT64),
  CAST(NULL AS INT64), CAST(NULL AS INT64),
  b.conversation_id AS bot_conversation_id
FROM bot b
WHERE b.channel = 'chat'
  AND NOT EXISTS (SELECT 1 FROM tickets t WHERE t.bot_link_key = b.platform_conversation_id)
)
-- One event label per event, whichever source named it: Zendesk's form field ("Chicago, IL"),
-- the bot's session variable ("ai_chicago" / "Chicago") or the bot's ticket tag.
SELECT
  * EXCEPT (event_raw),
  CASE
    WHEN event_raw IS NULL OR LOWER(event_raw) IN ('x', '') THEN NULL
    WHEN REGEXP_CONTAINS(LOWER(event_raw), r'chicago') THEN 'Chicago'
    WHEN REGEXP_CONTAINS(LOWER(event_raw), r'toronto|ttf|10kto') THEN 'Toronto'
    WHEN REGEXP_CONTAINS(LOWER(event_raw), r'kerrville') THEN 'Kerrville'
    WHEN REGEXP_CONTAINS(LOWER(event_raw), r'blenheim') THEN 'Blenheim'
    WHEN REGEXP_CONTAINS(LOWER(event_raw), r'long.?beach') THEN 'Long Beach'
    WHEN REGEXP_CONTAINS(LOWER(event_raw), r'new.?jersey') THEN 'New Jersey'
    WHEN REGEXP_CONTAINS(LOWER(event_raw), r'austin') THEN 'Austin'
    WHEN REGEXP_CONTAINS(LOWER(event_raw), r'toulouse') THEN 'Toulouse'
    WHEN REGEXP_CONTAINS(LOWER(event_raw), r'brighton') THEN 'Brighton'
    WHEN REGEXP_CONTAINS(LOWER(event_raw), r'chantilly') THEN 'Chantilly'
    WHEN REGEXP_CONTAINS(LOWER(event_raw), r'new.?york') THEN 'New York'
    WHEN REGEXP_CONTAINS(LOWER(event_raw), r'general|other|not specific') THEN 'General'
    ELSE event_raw
  END AS event
FROM unioned
;;

-- v_question_topics — the decision list: per month x channel x topic, how many questions came
-- in, how many the bot answered vs didn't, and how fast humans replied. 'no_question' tickets
-- (auto-replies, thank-yous) are counted separately and kept out of the answered share. A topic with high
-- volume and a low answered share is where adding bot knowledge pays off most.
-- Topic = our LLM's primary_topic for tickets; for bot-only chats (no text) the first
-- help-centre article the bot answered from, else '(bot-only, no article)'.
CREATE OR REPLACE VIEW `${DS}.v_question_topics` AS
SELECT
  DATE_TRUNC(question_date, MONTH) AS month,
  channel,
  COALESCE(primary_topic,
           IF(source = 'bot_only', CONCAT('article: ', bot_articles[SAFE_OFFSET(0)]), NULL),
           IF(source = 'bot_only', '(bot-only, no article)', '(not yet scored)')) AS topic,
  COUNTIF(IFNULL(our_resolution, '') != 'no_question') AS questions,
  COUNTIF(our_resolution = 'no_question') AS not_a_question,
  COUNTIF(source = 'bot_only') AS bot_only_questions,
  COUNTIF(bot_involved) AS bot_involved,
  COUNTIF(our_resolution = 'resolved'
          OR (our_resolution IS NULL AND zendesk_automated_resolution)) AS bot_answered,
  COUNTIF(our_resolution IN ('unresolved', 'partial')) AS not_fully_answered,
  COUNTIF(ended_with_human) AS reached_a_human,
  SAFE_DIVIDE(COUNTIF(our_resolution = 'resolved' OR (our_resolution IS NULL AND zendesk_automated_resolution)),
              COUNTIF(IFNULL(our_resolution, '') != 'no_question')) AS answered_share,
  APPROX_QUANTILES(reply_time_min, 2)[SAFE_OFFSET(1)] AS median_first_reply_min,
  APPROX_QUANTILES(full_resolution_time_min, 2)[SAFE_OFFSET(1)] AS median_full_resolution_min,
  COUNTIF(reopens > 0) AS reopened
FROM `${DS}.v_questions`
GROUP BY 1, 2, 3
;;

-- zendesk_dash.v_support_questions — the ONLY Zendesk object the management dashboard reads.
-- Lives in its own dataset (zendesk_dash) and is an AUTHORIZED view on zendesk_bot, so the
-- dashboard's service account can read this and nothing else: no ticket ids, subjects, tags,
-- transcripts or bot reasoning. One row per question; the dashboard aggregates.
CREATE OR REPLACE VIEW `${DASH}.v_support_questions` AS
SELECT
  question_date,
  DATE_TRUNC(question_date, MONTH) AS month,
  source,
  CASE
    WHEN source = 'bot_only' THEN 'Bot chat, no ticket'
    WHEN channel = 'email' THEN 'Email'
    WHEN channel = 'web' THEN 'Web form'
    WHEN channel = 'messaging' THEN 'Chat to a person'
    ELSE channel
  END AS channel_group,
  event,
  COALESCE(primary_topic,
           IF(source = 'bot_only', CONCAT('Article: ', bot_articles[SAFE_OFFSET(0)]), NULL)) AS topic,
  our_resolution,
  resolution_path,
  IFNULL(our_resolution, '') != 'no_question' AS is_question,
  (our_resolution = 'resolved' OR (our_resolution IS NULL AND IFNULL(zendesk_automated_resolution, FALSE))) AS bot_answered,
  our_resolution IN ('partial', 'unresolved') AS not_fully_answered,
  bot_involved,
  ended_with_human,
  ARRAY_LENGTH(bot_articles) > 0 AS bot_used_article,
  IFNULL(bot_not_understood, 0) > 0 AS bot_not_understood,
  IFNULL(bot_first_timer_signal, FALSE) AS first_timer_signal,
  reply_time_min,
  full_resolution_time_min,
  IFNULL(reopens, 0) > 0 AS reopened
FROM `${DS}.v_questions`
