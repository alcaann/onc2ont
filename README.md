# Output Format

This section describes the output format for the processed medical phrases. Each phrase is represented as a JSON object with the following structure:

```json
{
    "id": "phrase_001", // Unique ID for the phrase
    "text": "Pt presents with stage II NSCLC.",
    "tokens": ["Pt", "presents", "with", "stage", "II", "NSCLC", "."], // Tokenized text
    "entities": [
      {
        "id": "E1", // Unique ID within the phrase
        "text": "stage II NSCLC", // The text span
        "start_char": 17, // Character start index in original text
        "end_char": 31, // Character end index
        "start_token": 3, // Token start index
        "end_token": 5, // Token end index (inclusive)
        "label": "DISEASE", // Your NER label (initially generic)
        "snomed_ct_id": "placeholder_snomed_for_stage_II_NSCLC" // Placeholder!
      }
      // ... more entities
    ],
    "relations": [
      // Example if you had two entities like "NSCLC" and "stage II"
      // {
      //   "subj": "E2", // ID of the subject entity (e.g., NSCLC)
      //   "obj": "E1", // ID of the object entity (e.g., stage II)
      //   "label": "HAS_STAGE" // Your relation label
      // }
    ],
    "bio_tags": ["O", "O", "O", "B-DISEASE", "I-DISEASE", "I-DISEASE", "O"] // BIO format for NER
  }