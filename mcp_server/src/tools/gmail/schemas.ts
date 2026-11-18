import { z } from "zod";
import { emailArraySchema } from "../../validation/email.validator.js";
import { subjectSchema, bodySchema } from "../../validation/common.validator.js";

/**
 * Zod input schema for the `gmail_create_draft` tool.
 */
export const GmailCreateDraftSchema = z.object({
  // Optional: a draft may have no recipient yet. The person who sends it adds one in Gmail.
  to: emailArraySchema.optional().default([]),
  cc: emailArraySchema.optional().default([]),
  bcc: emailArraySchema.optional().default([]),
  subject: subjectSchema,
  body: bodySchema,
  body_type: z.enum(["plain", "html"]).optional().default("plain"),
});

export type GmailCreateDraftInput = z.infer<typeof GmailCreateDraftSchema>;
