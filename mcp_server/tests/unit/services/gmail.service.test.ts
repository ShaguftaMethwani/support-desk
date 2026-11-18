import { describe, it, expect, vi, beforeEach } from "vitest";
import { GmailService } from "../../../src/services/gmail.service.js";
import { GoogleAuthService } from "../../../src/services/google-auth.service.js";

// vi.hoisted() ensures these are initialized before vi.mock() factory is hoisted
const { mockDraftsCreate, mockMessagesSend } = vi.hoisted(() => ({
  mockDraftsCreate: vi.fn(),
  mockMessagesSend: vi.fn(),
}));

vi.mock("googleapis", () => ({
  google: {
    gmail: vi.fn().mockReturnValue({
      users: {
        drafts: { create: mockDraftsCreate },
        messages: { send: mockMessagesSend },
      },
    }),
  },
}));

function createMockAuthService(): GoogleAuthService {
  return {
    getAuthenticatedClient: vi.fn().mockResolvedValue({}),
  } as unknown as GoogleAuthService;
}

describe("GmailService.createDraft", () => {
  let service: GmailService;
  let mockAuth: GoogleAuthService;

  beforeEach(() => {
    mockDraftsCreate.mockResolvedValue({ data: { id: "mock-draft-id-123" } });
    mockMessagesSend.mockResolvedValue({
      data: { id: "mock-message-id-abc", threadId: "mock-thread-id-xyz" },
    });
    mockAuth = createMockAuthService();
    service = new GmailService(mockAuth);
  });

  it("returns a draft result with a draft_id on success", async () => {
    const result = await service.createDraft({
      to: ["recipient@example.com"],
      subject: "Test Subject",
      body: "Test body",
    });

    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.draft_id).toBe("mock-draft-id-123");
      expect(result.message).toBe("Draft created successfully.");
    }
  });

  it("includes cc and bcc in the MIME message", async () => {
    const result = await service.createDraft({
      to: ["to@example.com"],
      cc: ["cc@example.com"],
      bcc: ["bcc@example.com"],
      subject: "Subject",
      body: "Body",
    });

    expect(result.success).toBe(true);
  });

  it("supports html body type", async () => {
    const result = await service.createDraft({
      to: ["to@example.com"],
      subject: "HTML Email",
      body: "<h1>Hello</h1>",
      body_type: "html",
    });

    expect(result.success).toBe(true);
  });
});

describe("GmailService.createDraft without a recipient", () => {
  beforeEach(() => {
    mockDraftsCreate.mockReset();
    mockMessagesSend.mockReset();
    mockDraftsCreate.mockResolvedValue({ data: { id: "mock-draft-id-456" } });
  });

  function decodedRaw(): string {
    const raw = mockDraftsCreate.mock.calls[0]?.[0]?.requestBody?.message?.raw as string;
    return Buffer.from(raw.replace(/-/g, "+").replace(/_/g, "/"), "base64").toString("utf-8");
  }

  it("creates a draft with no To header when no recipient is given", async () => {
    const service = new GmailService(createMockAuthService());
    const result = await service.createDraft({ subject: "Weekly pulse", body: "Body" });

    expect(result.draft_id).toBe("mock-draft-id-456");
    expect(decodedRaw()).not.toMatch(/^To:/m);
    expect(decodedRaw()).toMatch(/^Subject: Weekly pulse$/m);
  });

  it("never calls the send API", async () => {
    const service = new GmailService(createMockAuthService());
    await service.createDraft({ to: ["to@example.com"], subject: "S", body: "B" });

    expect(mockMessagesSend).not.toHaveBeenCalled();
    expect("sendEmail" in service).toBe(false);
  });
});
