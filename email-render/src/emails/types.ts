export type NotifyChangeType = "NEW_DATE" | "UPDATED_SAME_DATE";

export type TldrNormalized =
  | { kind: "paragraph"; text: string }
  | { kind: "bullets"; lead: string | null; bullets: string[] };

export type NewsFeedEmailProps = {
  changeType: NotifyChangeType;
  dateRaw: string;
  items: string[];
  tldr: TldrNormalized | null;
  previewText: string;
};
