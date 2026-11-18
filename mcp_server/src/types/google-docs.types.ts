/**
 * Google Docs-specific TypeScript types for inputs and service responses.
 */

export interface GoogleDocsAppendInput {
  document_id: string;
  content: string;
  add_newline?: boolean;
  /** Idempotency key contained in `content`; the append is skipped if the doc has it. */
  dedupe_key?: string;
}

export interface GoogleDocsAppendResult {
  success: true;
  document_id: string;
  /** The document's revision after the append (or the current one, when skipped). */
  revision_id: string | null;
  /** True when dedupe_key was already in the document and nothing was written. */
  skipped: boolean;
  message: string;
  [key: string]: unknown;
}

/**
 * Represents the end-of-document index information fetched before appending.
 */
export interface DocumentEndIndex {
  documentId: string;
  endIndex: number;
}
