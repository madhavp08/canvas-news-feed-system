export type NotifyChangeType = "NEW_DATE" | "UPDATED_SAME_DATE";

export type TldrNormalized =
  | { kind: "paragraph"; text: string }
  | { kind: "bullets"; lead: string | null; bullets: string[] };

export type ItemSegment =
  | { type: "text"; text: string }
  | { type: "link"; href: string; label: string };

export type NewsFeedEmailProps = {
  changeType: NotifyChangeType;
  dateRaw: string;
  /** One array of segments per bulletin line */
  items: ItemSegment[][];
  tldr: TldrNormalized | null;
  previewText: string;
  /** Optional promo line from EMAIL_PROMO_TEXT; null to omit the callout */
  promoText: string | null;
};
