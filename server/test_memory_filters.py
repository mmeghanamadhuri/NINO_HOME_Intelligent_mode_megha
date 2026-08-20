"""Tests for memory logging gates and validators."""

from __future__ import annotations

import unittest

from memory_filters import (
    RECALL_ALL_PREFERENCES,
    RECALL_DISLIKES,
    canonical_preference_key,
    conversation_log_skip_reason,
    filter_memories_for_query,
    format_memory_for_recall,
    infer_memory_key,
    infer_recall_memory_key,
    is_alarm_followup_question,
    is_alarm_or_reminder_command,
    is_ephemeral_query,
    is_junk_memory_text,
    is_memory_recall_question,
    is_preference_update_statement,
    is_trivia_query,
    is_valid_memory_text,
    user_explicitly_states_personal_fact,
    memory_extract_skip_reason,
    parse_like_dislike_update,
    parse_preference_update,
    query_needs_recent_context,
    should_extract_memories,
)


class FollowUpContextTests(unittest.TestCase):
    def test_standalone_questions_do_not_need_recent_context(self) -> None:
        for text in (
            "Which planet has a ring?",
            "What's the weather today?",
            "Can you explain the Solar System?",
            "Which is the largest plant?",
            "Football is my favorite sport.",
            "What are you doing?",
        ):
            with self.subTest(text=text):
                self.assertFalse(query_needs_recent_context(text), msg=text)

    def test_followups_need_recent_context(self) -> None:
        for text in (
            "Tell me more about it.",
            "What about Saturn?",
            "And then what happened?",
            "How does that work?",
            "Explain that again.",
            "Explain more.",
            "Why is that?",
            "What is it made of?",
            "the Mars.",
            "Mars.",
        ):
            with self.subTest(text=text):
                self.assertTrue(query_needs_recent_context(text), msg=text)


class LastQuestionRecallTests(unittest.TestCase):
    def test_last_question_phrases_detected(self) -> None:
        from llm_service import is_last_question_query

        for text in (
            "What was my last question?",
            "what's my last question",
            "What was the last thing I asked?",
            "Remind me of my last question",
            "What did I just ask?",
            "That's what I wanted to know from you. I asked you what is my last question.",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_last_question_query(text), msg=text)

    def test_last_question_reply_uses_stored_turn(self) -> None:
        from llm_service import (
            answer_last_user_question,
            last_user_question_from_history,
        )

        history = [
            ("Tell me about Mars.", "Mars is the red planet."),
            ("Tell me about Planet Earth.", "Earth is the blue marble."),
            ("Why is the Mars called Red Planet?", "Because of iron oxide."),
        ]
        last = last_user_question_from_history(history)
        self.assertEqual(last, "Why is the Mars called Red Planet?")
        reply = answer_last_user_question(last, viewer_name="Kartik", has_face=True)
        self.assertIn("Kartik", reply)
        self.assertIn("Why is the Mars called Red Planet?", reply)
        self.assertNotIn("status and availability", reply.lower())

    def test_last_question_skips_recap_turns(self) -> None:
        from llm_service import last_user_question_from_history

        history = [
            ("Tell me about Mars.", "Mars is dusty and red."),
            ("What was my last question?", "Your previous request was about status."),
        ]
        self.assertEqual(
            last_user_question_from_history(history),
            "Tell me about Mars.",
        )

    def test_last_question_skips_comments_jokes_and_meta(self) -> None:
        from llm_service import last_user_question_from_history

        history = [
            ("Okay, is it closer to Neptune?", "Pluto is near Neptune."),
            ("That's really long.", "The Earth began as a giant cloud."),
            ("Tell me a joke.", "Parallel lines have so much in common."),
            ("What was my last question?", "Kartik, you asked that's really long."),
            ("That was not my question.", "What were you looking for?"),
            (
                "That's what I wanted to know from you. I asked you what is my last question.",
                "Kartik, you asked that was not my question.",
            ),
        ]
        self.assertEqual(
            last_user_question_from_history(history),
            "Okay, is it closer to Neptune?",
        )

    def test_last_question_without_face(self) -> None:
        from llm_service import answer_last_user_question

        reply = answer_last_user_question(
            "Tell me about Mars.",
            viewer_name=None,
            has_face=False,
        )
        self.assertIn("camera", reply.lower())


class MemoryFilterTests(unittest.TestCase):
    def test_joke_not_logged(self) -> None:
        for text in (
            "Tell me a joke.",
            "a joke.",
            "tell a joke",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_ephemeral_query(text), msg=text)
                self.assertEqual(
                    conversation_log_skip_reason(text),
                    "skipped_ephemeral",
                )
        self.assertEqual(conversation_log_skip_reason("joke."), "skipped_fragment")

    def test_denylist_only_skips_noise(self) -> None:
        """Default is store — any topic question should log without a topic allow-list."""
        for text in (
            "Tell me how does a GPU works?",
            "me how does a mic works.",
            "Tell me how does a speaker works?",
            "What is the difference between LED display and LCD display?",
            "Please define what is a micro country.",
            "How does a processor controller work?",
            "and I learned to meet my friend Dimple",
            "Happy celebrate my birthday",
            "Love to prefer coffee more than a tea.",
        ):
            with self.subTest(text=text):
                self.assertIsNone(conversation_log_skip_reason(text))

    def test_vision_greeting_paths_are_logged(self) -> None:
        for path in ("vision_greeting", "startup_greeting"):
            with self.subTest(path=path):
                self.assertIsNone(
                    conversation_log_skip_reason(
                        "[face recognized]", reply_path=path
                    )
                )

    def test_questions_not_extracted_as_long_term_memory(self) -> None:
        for text in (
            "Tell me how does a GPU works?",
            "How does a processor controller work?",
            "Please define what is a micro country.",
        ):
            with self.subTest(text=text):
                self.assertEqual(memory_extract_skip_reason(text), "skipped_question")

    def test_recap_phrases_detected(self) -> None:
        from llm_service import (
            extract_recap_focus_topic,
            is_conversation_recap_question,
            recap_turn_matches_topic,
            user_requests_topic_brief,
        )

        for text in (
            "In this view, the recap of what we are talking right now",
            "what we are discussing.",
            "Please give me the context of what we are talking right now.",
            "Give me the context.",
            "So we are talking about CEOs of India. Aren't we?",
            "Hope that we are talking about CEO of India's",
            "Are we talking about CEOs of India?",
            "What was my last question?",
            "What is my last question?",
            "What was the last thing I asked?",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_conversation_recap_question(text))
                self.assertEqual(conversation_log_skip_reason(text), "skipped_recap")

        topic = extract_recap_focus_topic(
            "Hope we are talking about something called microcontroller."
        )
        self.assertEqual(topic, "microcontroller")
        topic2 = extract_recap_focus_topic(
            "Hope we are talking about heights and distances right? Could you please brief out about that?"
        )
        self.assertEqual(topic2, "heights and distances")
        self.assertTrue(user_requests_topic_brief(
            "Hope we are talking about heights and distances right? Could you please brief out about that?"
        ))
        self.assertFalse(
            recap_turn_matches_topic(
                "microcontroller",
                "My favorite food is biryani.",
                "Sounds delicious!",
            )
        )
        self.assertTrue(
            recap_turn_matches_topic(
                "microcontroller",
                "How does a microcontroller work?",
                "It is a small computer on a chip.",
            )
        )

    def test_assumed_prior_topic_detected(self) -> None:
        from llm_service import (
            extract_recap_focus_topic,
            is_assumed_prior_topic_question,
            recap_topic_not_found_reply,
        )

        cases = (
            (
                "So, we are talking about trigonometry right? Could you please brief out that?",
                "trigonometry",
            ),
            (
                "So, yesterday we are talking about a race in sea programming right? Could you please brief about that?",
                "race in sea programming",
            ),
            (
                "Today we are talking about arrays write and see programming. Could you please brief out that?",
                "arrays write and see programming",
            ),
        )
        for text, expected_topic in cases:
            with self.subTest(text=text[:50]):
                self.assertTrue(is_assumed_prior_topic_question(text))
                self.assertEqual(extract_recap_focus_topic(text), expected_topic)

        reply = recap_topic_not_found_reply("trigonometry", person_name="Chakri")
        self.assertIn("don't have trigonometry", reply.lower())
        self.assertIn("shall we discuss it now", reply.lower())

    def test_past_time_recap_phrases_from_latency_log(self) -> None:
        from llm_service import (
            extract_recap_focus_topic,
            is_assumed_prior_topic_question,
            is_conversation_recap_question,
        )

        cases = (
            (
                "We are talking about a trigonometry, right?",
                True,
                True,
                "trigonometry",
            ),
            (
                "Today we are talking about speakers and mics right? Could you please explain about that?",
                True,
                True,
                "speakers and mics",
            ),
            (
                "Few minutes back we had a discussion on speakers right? Please explain about that again.",
                True,
                True,
                "speakers",
            ),
            (
                "Earlier today we discussed microcontrollers. Could you brief that again?",
                True,
                True,
                "microcontrollers",
            ),
            (
                "A while ago we had a conversation about sea programming right?",
                True,
                True,
                "sea programming",
            ),
        )
        for text, want_recap, want_topic_focus, expected_topic in cases:
            with self.subTest(text=text[:55]):
                self.assertEqual(
                    is_conversation_recap_question(text),
                    want_recap,
                    msg=f"recap detect: {text}",
                )
                self.assertEqual(
                    is_assumed_prior_topic_question(text),
                    want_topic_focus,
                    msg=f"topic focus: {text}",
                )
                self.assertEqual(
                    extract_recap_focus_topic(text),
                    expected_topic,
                    msg=f"topic extract: {text}",
                )

    def test_recap_follow_up_question_detection(self) -> None:
        from llm_service import (
            extract_recap_follow_up_question,
            is_recap_with_follow_up_question,
        )

        compound = (
            "Hi, we are talking about speakers right? "
            "And on top of that add, what components are used in that?"
        )
        self.assertTrue(is_recap_with_follow_up_question(compound))
        follow_up = extract_recap_follow_up_question(compound)
        self.assertIsNotNone(follow_up)
        assert follow_up is not None
        self.assertIn("components", follow_up.lower())

        brief_only = (
            "So, we are talking about trigonometry right? Could you please brief out that?"
        )
        self.assertFalse(is_recap_with_follow_up_question(brief_only))
        self.assertIsNone(extract_recap_follow_up_question(brief_only))

    def test_latency_log_speakers_compound_no_question_mark(self) -> None:
        from llm_service import (
            extract_recap_focus_topic,
            extract_recap_follow_up_question,
            recap_turn_matches_topic,
        )

        heard = (
            "we are talking about speakers right could you please brief about that "
            "and what components are used in"
        )
        self.assertEqual(extract_recap_focus_topic(heard), "speakers")
        follow_up = extract_recap_follow_up_question(heard)
        self.assertIsNotNone(follow_up)
        assert follow_up is not None
        self.assertIn("components", follow_up.lower())

        prior_user = (
            "Few minutes back we had a discussion on speakers right? "
            "Please explain about that again."
        )
        prior_reply = (
            "Of course! A speaker is just like a microphone but usually attached "
            "to something permanent in the room."
        )
        self.assertTrue(recap_turn_matches_topic("speakers", prior_user, prior_reply))

    def test_memory_recall_not_logged(self) -> None:
        self.assertEqual(
            conversation_log_skip_reason("What do I prefer?"),
            "skipped_recall",
        )

    def test_tts_echo_not_logged(self) -> None:
        for text in (
            "The birth date is on July 13, 2003.",
            "Born on July 13, 2003",
        ):
            with self.subTest(text=text):
                self.assertEqual(conversation_log_skip_reason(text), "skipped_tts_echo")

    def test_alarm_followup_detected(self) -> None:
        text = "You are telling me to take medicines at night?"
        self.assertTrue(is_alarm_followup_question(text))
        self.assertEqual(
            conversation_log_skip_reason(text),
            "skipped_alarm_followup",
        )

    def test_alarm_commands_not_logged(self) -> None:
        text = "Find me to take medicines at 6.22pm today."
        self.assertTrue(is_alarm_or_reminder_command(text))
        self.assertEqual(
            conversation_log_skip_reason(text, reply_path="llm"),
            "skipped_alarm_command",
        )
        self.assertFalse(should_extract_memories(text, reply_path="llm"))

    def test_personal_fact_extracted(self) -> None:
        self.assertTrue(should_extract_memories("I work as a software engineer.", reply_path="llm"))
        self.assertTrue(should_extract_memories("I prefer tea over coffee now.", reply_path="llm"))

    def test_hallucinated_memory_not_valid(self) -> None:
        self.assertFalse(
            is_valid_memory_text(
                "Reading and playing video games are my hobbies.",
                user_text="How to play indoor games than outdoor games.",
                assistant_text="You can try board games indoors.",
            )
        )
        self.assertTrue(is_junk_memory_text("My birthday is on [insert actual date here]."))

    def test_grounded_memory_valid(self) -> None:
        self.assertTrue(
            is_valid_memory_text(
                "Chakri prefers tea over coffee.",
                user_text="I prefer tea over coffee now.",
                assistant_text="Got it, you prefer tea.",
            )
        )

    def test_joke_not_extracted(self) -> None:
        self.assertFalse(should_extract_memories("Tell me a joke.", reply_path="llm"))

    def test_junk_memory_rejected(self) -> None:
        self.assertTrue(is_junk_memory_text("i"))
        self.assertTrue(is_junk_memory_text("Don't believe everything you hear about birthdays!"))

    def test_preference_key_inference(self) -> None:
        self.assertEqual(infer_memory_key("Favorite beverage is tea"), "favorite_drink")
        self.assertEqual(infer_memory_key("Birthdate: July 13th, 2003"), "birthdate")
        self.assertEqual(canonical_preference_key("soft drink"), "favorite_drink")
        self.assertEqual(canonical_preference_key("sport"), "favorite_sport")

    def test_drink_and_sport_recall_keys(self) -> None:
        self.assertEqual(infer_recall_memory_key("What is my favorite drink?"), "favorite_drink")
        self.assertEqual(
            infer_recall_memory_key("What is my favorite soft drink?"),
            "favorite_drink",
        )
        self.assertEqual(infer_recall_memory_key("What is my favorite sport?"), "favorite_sport")
        self.assertEqual(
            infer_recall_memory_key("What do I prefer to drink? Tea or coffee?"),
            "favorite_drink",
        )
        self.assertTrue(is_memory_recall_question("Please my favorite soft drink"))

    def test_medicine_query_filters_birthday_facts(self) -> None:
        memories = [
            "Chakri was not born on June 25th",
            "Favorite game is chess",
            "Take medicines at 3:52 AM",
        ]
        filtered = filter_memories_for_query(
            memories,
            "You are telling me to take medicines at night?",
        )
        self.assertTrue(any("medicine" in m.lower() or "3:52" in m for m in filtered))
        self.assertFalse(any("june 25" in m.lower() for m in filtered))

    def test_trivia_helper_still_detects_known_patterns(self) -> None:
        self.assertTrue(is_trivia_query("Tell me how does a GPU works?"))

    def test_full_form_queries_are_trivia_not_memory(self) -> None:
        for text in (
            "Full form of Wi-Fi.",
            "What is the full form of CPU and GPU?",
            "What is the full form of CNN?",
            "Ah... What is the full form of CNN in image processing?",
            "spell password",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_trivia_query(text), msg=text)
                self.assertFalse(is_preference_update_statement(text), msg=text)
                self.assertFalse(user_explicitly_states_personal_fact(text), msg=text)

    def test_favorite_food_recall_detected(self) -> None:
        for text in (
            "What is my favourite food?",
            "What's my favorite food?",
            "What is my favorite food?",
            "This is my favorite food.",
            "It is my favorite food.",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_memory_recall_question(text))
                self.assertEqual(infer_recall_memory_key(text), "favorite_food")
                self.assertEqual(conversation_log_skip_reason(text), "skipped_recall")

    def test_reversed_favorite_food_update(self) -> None:
        text = "Lemon rice is my favorite food."
        self.assertFalse(is_memory_recall_question(text))
        self.assertTrue(is_preference_update_statement(text))
        self.assertEqual(parse_preference_update(text), ("food", "Lemon rice"))

    def test_like_dislike_recall_and_update(self) -> None:
        self.assertTrue(is_memory_recall_question("What do I like?"))
        self.assertEqual(infer_recall_memory_key("What do I like?"), "__all_preferences__")
        self.assertTrue(is_memory_recall_question("What don't I like?"))
        self.assertEqual(infer_recall_memory_key("What don't I like?"), "__dislikes__")
        self.assertTrue(is_preference_update_statement("I hate spicy food."))
        self.assertEqual(parse_like_dislike_update("I hate spicy food."), ("dislike", "spicy food"))

    def test_preference_update_parsed_and_skips_async_extract(self) -> None:
        text = "My favorite food is Idli."
        self.assertTrue(is_preference_update_statement(text))
        self.assertEqual(parse_preference_update(text), ("food", "Idli"))
        self.assertFalse(should_extract_memories(text, reply_path="memory_llm_store"))

    def test_format_memory_for_recall_favorite_food(self) -> None:
        self.assertEqual(
            format_memory_for_recall("Idli is my favorite food"),
            "Your favorite food is Idli.",
        )
        self.assertEqual(
            format_memory_for_recall("coffee is my favorite drink"),
            "Your favorite drink is coffee.",
        )
        self.assertEqual(
            format_memory_for_recall("My birthday is on 25th June"),
            "Your birthday is 25th June.",
        )

    def test_birthdate_recall_and_update(self) -> None:
        from memory_filters import (
            enrich_llm_memory_text,
            is_likely_tts_echo,
            is_unintelligible_stt,
            is_valid_llm_memory_item,
            parse_birthdate_update,
            user_explicitly_states_personal_fact,
        )

        self.assertTrue(is_memory_recall_question("Miss my birthday!"))
        self.assertEqual(infer_recall_memory_key("What is my birthday?"), "birthdate")
        self.assertEqual(parse_birthdate_update("Birthday is on 24th."), "24th")
        self.assertEqual(
            parse_birthdate_update("Birth date is on 25th June."), "25th June"
        )
        self.assertTrue(is_likely_tts_echo("Your birthday is June 25th."))
        self.assertFalse(is_likely_tts_echo("Birthday is on 24th."))
        self.assertTrue(is_unintelligible_stt("***"))
        # ElevenLabs often maps short noise to non-Latin script — reject for UK English demo.
        self.assertTrue(is_unintelligible_stt("పాకుండాయి"))
        self.assertTrue(is_unintelligible_stt("नमस्ते क्या हाल है"))
        self.assertFalse(is_unintelligible_stt("What's the weather today?"))
        self.assertFalse(is_unintelligible_stt("café"))
        from memory_filters import is_whisper_silence_hallucination

        self.assertTrue(
            is_whisper_silence_hallucination(
                "Thank you.",
                mean_energy=15,
                peak_energy=90,
                audio_seconds=30.0,
            )
        )
        self.assertFalse(
            is_whisper_silence_hallucination(
                "Thank you.",
                mean_energy=40,
                peak_energy=200,
                audio_seconds=2.0,
            )
        )
        self.assertFalse(
            is_whisper_silence_hallucination(
                "What's the weather today?",
                mean_energy=10,
                peak_energy=50,
                audio_seconds=30.0,
            )
        )
        self.assertTrue(is_preference_update_statement("My birthday is June 25th."))

    def test_llm_memory_enrichment_for_terse_extractions(self) -> None:
        from memory_filters import enrich_llm_memory_text, is_valid_llm_memory_item

        user = "Favorite food is Biryani not lemon rice."
        self.assertTrue(user_explicitly_states_personal_fact(user))
        enriched = enrich_llm_memory_text("Biryani", "favorite_food", user)
        self.assertIn("biryani", enriched.lower())
        self.assertTrue(
            is_valid_llm_memory_item(enriched, user_text=user, memory_key="favorite_food")
        )


if __name__ == "__main__":
    unittest.main()
