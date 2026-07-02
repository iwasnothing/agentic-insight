---
name: doc-retrieval-skill
description: Retrieve and evaluate concept definitions and unstructured segments from the document database.
---

# Document Retrieval Skill

Use this skill when you need to search document concepts, select relevant candidate concepts, retrieve text segments, and analyze unstructured document context.

## Document Search Planning
- Decompose the input query string into a list of specific sub-queries.
- Each sub-query must target a specific concept type from the available ontology schema and define a precise sub-query search string.
- DO NOT write general search strings that query all concepts or all data. Target a precise research objective.

## Concept Selection / Evaluation
- Evaluate candidate concepts against the sub-query string.
- Select ALL concepts from the candidate list that are related to the query string to maximize recall.

## Unstructured Document Context Analysis
- Review and analyze the accumulated unstructured concept definitions and source segment texts.
- Answer the search query using the provided context and format the output in Markdown.
