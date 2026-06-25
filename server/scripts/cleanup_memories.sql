-- Purge polluted memory rows (LLM hallucinations, jokes, STT/TTS fragments).
DELETE FROM memories
WHERE LENGTH(TRIM(memory_text)) < 8
   OR TRIM(memory_text) IN ('i', 'you')
   OR memory_text ILIKE '%don''t believe everything%'
   OR memory_text ILIKE '%not born on june%'
   OR memory_text ILIKE '%just a joke%'
   OR memory_text ILIKE '%intelligent assistant designed%'
   OR memory_text ILIKE 'currently working on projects%'
   OR memory_text ILIKE '%[insert%'
   OR memory_text ILIKE '%insert actual date%'
   OR memory_text ILIKE '%are my hobbies%'
   OR memory_text ILIKE '%preferred beverage%'
   OR memory_text ILIKE '%reading and playing video games%'
   OR memory_text ILIKE '%enjoy playing board games%';

-- Remove non-conversation Q&A turns (definitions, recall, greetings, TTS echo).
DELETE FROM conversations
WHERE user_text ILIKE 'please define%'
   OR user_text ILIKE 'detail explain%'
   OR user_text ILIKE 'this is a microprocessor%'
   OR user_text ILIKE 'the birth date is%'
   OR user_text ILIKE 'born on july%'
   OR user_text ILIKE 'what do i prefer%'
   OR user_text ILIKE 'do i prefer%'
   OR user_text ILIKE 'happy celebrate%'
   OR user_text ILIKE 'how to play indoor%'
   OR user_text ILIKE 'find me to take medicines%'
   OR user_text ILIKE 'love to prefer coffee%'
   OR user_text ILIKE 'love tea more than coffee%'
   OR user_text ILIKE 'it''s my birthday%'
   OR user_text ILIKE 'i miss my butter%'
   OR user_text ILIKE 'i would love tea%';
