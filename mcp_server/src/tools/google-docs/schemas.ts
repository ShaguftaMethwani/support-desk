import { z } from "zod";
import { documentIdSchema, appendContentSchema } from "../../validation/common.validator.js";

/**
 * Zod input schema for the `google_docs_append` tool.
 */
export const GoogleDocsAppendSchema = z.object({
  document_id: documentIdSchema,
  content: appendContentSchema,
  add_newline: z
    .boolean()
    .optional()
    .default(false)
    .describe("When true, prepends a newline before the appended content."),
  dedupe_key: z
    .string()
    .min(8, "dedupe_key must be at least 8 characters.")
    .max(200, "dedupe_key must be at most 200 characters.")
    .optional()
    .describe(
      "Optional idempotency key that must appear in the content. If the document already " +
        "contains it, nothing is appended. Use it so a retry never writes the same entry twice."
    ),
});
// The service also checks that `content` contains `dedupe_key` (a refine() here would hide
// the object shape the tool registry needs).

export type GoogleDocsAppendInput = z.infer<typeof GoogleDocsAppendSchema>;
