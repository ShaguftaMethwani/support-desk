import { docs as docsApi } from "@googleapis/docs";
import { GoogleAuthService } from "./google-auth.service.js";
import { GoogleDocsAppendInput, GoogleDocsAppendResult } from "../types/google-docs.types.js";
import { logger } from "../logging/logger.js";
import { McpError } from "../errors/mcp-error.js";
import { ErrorCode } from "../errors/error-codes.js";

/**
 * Service encapsulating all Google Docs API interactions.
 *
 * Responsibilities:
 * - Fetch the current document structure to find the correct insertion index
 * - Construct batchUpdate insertText requests that preserve existing content
 * - Handle the Google Docs API's structural model correctly:
 *     The document body always ends with a trailing newline (\n) at `endIndex - 1`.
 *     We must insert BEFORE this trailing newline to avoid corrupting the structure.
 *
 * This service has NO knowledge of MCP — it speaks only in typed inputs/outputs.
 */
export class GoogleDocsService {
  private readonly authService: GoogleAuthService;

  constructor(authService: GoogleAuthService) {
    this.authService = authService;
  }

  /**
   * Appends text content to the end of an existing Google Doc.
   * Preserves all existing content.
   */
  async appendContent(input: GoogleDocsAppendInput): Promise<GoogleDocsAppendResult> {
    if (input.dedupe_key && !input.content.includes(input.dedupe_key)) {
      throw new McpError(
        ErrorCode.INVALID_INPUT,
        "content must contain dedupe_key, so a later append can find it."
      );
    }
    const auth = await this.authService.getAuthenticatedClient();
    const docs = docsApi({ version: "v1", auth });

    logger.info(
      { tool_name: "google_docs_append", operation: "documents.get", document_id: input.document_id },
      "Fetching document structure"
    );

    // Step 1: Fetch document to determine the insertion index
    let docResponse;
    try {
      docResponse = await docs.documents.get({
        documentId: input.document_id,
      });
    } catch (err: unknown) {
      // Re-map 404 specifically for documents
      const status = (err as { response?: { status?: number } })?.response?.status;
      if (status === 404) {
        throw new McpError(
          ErrorCode.DOCUMENT_NOT_FOUND,
          `Document "${input.document_id}" was not found or you do not have access to it.`
        );
      }
      throw err; // let the caller's error handler deal with it
    }

    const body = docResponse.data.body;
    const readRevision = docResponse.data.revisionId ?? null;
    if (!body || !body.content || body.content.length === 0) {
      throw new McpError(
        ErrorCode.INVALID_DOCUMENT,
        "The document has no readable body content. It may be empty or have an unsupported structure."
      );
    }

    // Step 2: Find the end index of the document body.
    //
    // Google Docs API: the body content array contains structural elements.
    // The document always ends with a trailing paragraph containing \n.
    // `endIndex` of the last element points AFTER the newline.
    // We must insert at `endIndex - 1` to place content before the trailing newline.
    const lastElement = body.content[body.content.length - 1];
    const rawEndIndex = lastElement?.endIndex;

    if (rawEndIndex === undefined || rawEndIndex === null) {
      throw new McpError(
        ErrorCode.INVALID_DOCUMENT,
        "Could not determine the document end index. The document structure may be unsupported."
      );
    }

    // Insert before the trailing newline (endIndex - 1)
    const insertionIndex = rawEndIndex - 1;

    if (insertionIndex < 1) {
      throw new McpError(
        ErrorCode.INVALID_DOCUMENT,
        "The document end index is invalid. The document may be malformed."
      );
    }

    // Step 3: Idempotency. If the document already contains the key, a previous attempt
    // (for example one whose response timed out) already wrote this entry.
    if (input.dedupe_key && documentText(body.content).includes(input.dedupe_key)) {
      logger.info(
        { tool_name: "google_docs_append", document_id: input.document_id, skipped: true },
        "Content with this dedupe_key is already in the document; nothing appended"
      );
      return {
        success: true,
        document_id: input.document_id,
        revision_id: readRevision,
        skipped: true,
        message: "Content already present (dedupe_key found); nothing appended.",
      };
    }

    // Step 4: Prepare the text to insert
    const textToInsert = input.add_newline ? `\n${input.content}` : input.content;

    logger.info(
      {
        tool_name: "google_docs_append",
        operation: "documents.batchUpdate",
        document_id: input.document_id,
        insertion_index: insertionIndex,
      },
      "Appending content to document"
    );

    // Step 5: Execute batchUpdate with insertText request. With a dedupe_key the write is
    // pinned to the revision just read, so a change in between makes it fail (and be retried)
    // instead of skipping the duplicate check or inserting at a stale index.
    const update = await docs.documents.batchUpdate({
      documentId: input.document_id,
      requestBody: {
        ...(input.dedupe_key && readRevision
          ? { writeControl: { requiredRevisionId: readRevision } }
          : {}),
        requests: [
          {
            insertText: {
              location: { index: insertionIndex },
              text: textToInsert,
            },
          },
        ],
      },
    });

    logger.info(
      {
        tool_name: "google_docs_append",
        operation: "documents.batchUpdate",
        document_id: input.document_id,
        success: true,
      },
      "Content appended to document"
    );

    return {
      success: true,
      document_id: input.document_id,
      revision_id: update.data.writeControl?.requiredRevisionId ?? null,
      skipped: false,
      message: "Content appended successfully.",
    };
  }
}

type StructuralElement = {
  paragraph?: { elements?: Array<{ textRun?: { content?: string | null } | null }> | null } | null;
  table?: { tableRows?: Array<{ tableCells?: Array<{ content?: StructuralElement[] | null }> | null }> | null } | null;
};

/** All plain text in the document body, including table cells. */
function documentText(content: StructuralElement[]): string {
  const parts: string[] = [];
  for (const element of content) {
    for (const run of element.paragraph?.elements ?? []) {
      if (run?.textRun?.content) parts.push(run.textRun.content);
    }
    for (const row of element.table?.tableRows ?? []) {
      for (const cell of row.tableCells ?? []) {
        parts.push(documentText(cell.content ?? []));
      }
    }
  }
  return parts.join("");
}
