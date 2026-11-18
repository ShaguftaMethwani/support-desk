import { vi } from "vitest";

/**
 * Mock factory for the Google Docs API client.
 */
export function createMockDocsClient(overrides?: {
  documentId?: string;
  endIndex?: number;
  shouldFailGet?: boolean;
  shouldFailUpdate?: boolean;
  failStatus?: number;
  revisionId?: string;
  revisionAfter?: string;
  text?: string;
}) {
  const opts = {
    documentId: "mock-doc-id-123",
    endIndex: 100,
    shouldFailGet: false,
    shouldFailUpdate: false,
    failStatus: 500,
    revisionId: "rev-1",
    revisionAfter: "rev-2",
    text: "Existing entry\n",
    ...overrides,
  };

  const mockDocumentBody = {
    content: [
      {
        startIndex: 0,
        endIndex: opts.endIndex,
        paragraph: { elements: [{ textRun: { content: opts.text } }] },
      },
    ],
  };

  const mockDocGet = opts.shouldFailGet
    ? vi.fn().mockRejectedValue(makeMockGaxiosError(opts.failStatus))
    : vi.fn().mockResolvedValue({
        data: {
          documentId: opts.documentId,
          revisionId: opts.revisionId,
          body: mockDocumentBody,
        },
      });

  const mockDocsBatchUpdate = opts.shouldFailUpdate
    ? vi.fn().mockRejectedValue(makeMockGaxiosError(opts.failStatus))
    : vi.fn().mockResolvedValue({
        data: { writeControl: { requiredRevisionId: opts.revisionAfter } },
      });

  return {
    documents: {
      get: mockDocGet,
      batchUpdate: mockDocsBatchUpdate,
    },
    _mocks: {
      docGet: mockDocGet,
      docsBatchUpdate: mockDocsBatchUpdate,
    },
  };
}

export function makeMockGaxiosError(status: number, message = "Google API Error") {
  const err = new Error(message) as Error & {
    response: { status: number; data: { error: { message: string } } };
  };
  err.response = { status, data: { error: { message } } };
  return err;
}
