import {
  Body,
  Column,
  Container,
  Head,
  Hr,
  Html,
  Img,
  Link,
  Row,
  Section,
  Text,
} from "@react-email/components";
import { Fragment } from "react";
import { EmailSummaryCard } from "./EmailSummaryCard";
import type { NewsFeedEmailProps, ItemSegment } from "./types";

const PROMO_URL_SPLIT = /(https?:\/\/[^\s]+)/gi;

/** TL;DR / ad-feedback shared surface styles (aligned with EmailSummaryCard). */
const TLDR_SURFACE_STYLE = {
  backgroundColor: "#FFB8B8",
  border: "1px solid #E85C5C",
} as const;

function promoHrefAllowed(href: string): boolean {
  const lc = href.trim().toLowerCase();
  return lc.startsWith("https://") || lc.startsWith("http://");
}

/** When *ctaHref* is set, every URL chunk links there (single CTA); labels stay the matched text. */
function PromoLinkified({
  text,
  ctaHref,
}: {
  text: string;
  ctaHref: string | null;
}) {
  const chunks = text.split(PROMO_URL_SPLIT).filter((c) => c.length > 0);
  return (
    <>
      {chunks.map((chunk, i) =>
        /^https?:\/\//i.test(chunk) && promoHrefAllowed(chunk) ? (
          <Link
            key={i}
            href={
              ctaHref && promoHrefAllowed(ctaHref) ? ctaHref : chunk.trim()
            }
            style={{
              color: "#0b57d0",
              textDecoration: "underline",
            }}
          >
            {chunk.trim()}
          </Link>
        ) : (
          <Fragment key={i}>{chunk}</Fragment>
        ),
      )}
    </>
  );
}

const promoBoxStyle = {
  border: "1px solid #e2e6ef",
  borderRadius: "10px",
  padding: "18px 18px",
  margin: "20px 0 0",
  backgroundColor: "#f9fafb",
} as const;

const promoTextStyle = {
  margin: 0,
  fontSize: "14px",
  lineHeight: "21px",
  color: "#2b303a",
  fontFamily: "Arial, Helvetica, sans-serif",
} as const;

/** Inline logo sized just above promo text cap height (~21px line). */
function PromoLogoImg({
  src,
  promoLinkHref,
}: {
  src: string;
  promoLinkHref: string | null;
}) {
  const img = (
    <Img
      src={src}
      alt="Thinkex"
      width={62}
      height={22}
      style={{
        display: "block",
        height: "22px",
        width: "auto",
        maxWidth: "72px",
        border: 0,
        outline: "none",
      }}
    />
  );

  return promoLinkHref && promoHrefAllowed(promoLinkHref) ? (
    <Link
      href={promoLinkHref}
      style={{ textDecoration: "none", border: "none", lineHeight: "1" }}
    >
      {img}
    </Link>
  ) : (
    img
  );
}

function PromoCallout({
  promoText,
  promoLogoUrl,
  promoLinkHref,
}: {
  promoText: string;
  promoLogoUrl: string | null;
  promoLinkHref: string | null;
}) {
  return (
    <Section className="promo-callout" style={promoBoxStyle}>
      <Row style={{ verticalAlign: "middle" }}>
        {promoLogoUrl ? (
          <Column
            style={{
              width: "74px",
              paddingRight: "10px",
              verticalAlign: "middle",
              lineHeight: "0",
            }}
            className="promo-logo-cell"
          >
            <PromoLogoImg src={promoLogoUrl} promoLinkHref={promoLinkHref} />
          </Column>
        ) : null}
        <Column style={{ verticalAlign: "middle", width: promoLogoUrl ? undefined : "100%" }}>
          <Text className="promo-text" style={{ ...promoTextStyle }}>
            <PromoLinkified text={promoText} ctaHref={promoLinkHref} />
          </Text>
        </Column>
      </Row>
    </Section>
  );
}

const AD_FEEDBACK_INNER_MARGIN = {
  padding: "16px 18px",
  margin: "12px 0 0",
  borderRadius: "8px",
  ...TLDR_SURFACE_STYLE,
} as const;

const adFeedbackOptStyle = {
  display: "inline-block",
  margin: "6px 8px 0 0",
  padding: "8px 12px",
  fontSize: "14px",
  lineHeight: "20px",
  fontFamily: "Arial, Helvetica, sans-serif",
  color: "#1a1d24",
  backgroundColor: "rgba(255,255,255,0.45)",
  border: "1px solid #cf4a4a",
  borderRadius: "999px",
  textDecoration: "none",
  fontWeight: 600 as const,
} as const;

function AdFeedbackMcq({
  adFeedbackBaseUrl,
}: {
  adFeedbackBaseUrl: string | null | undefined;
}) {
  const base =
    typeof adFeedbackBaseUrl === "string" ? adFeedbackBaseUrl.trim() : "";
  if (!base.startsWith("http://") && !base.startsWith("https://")) {
    return null;
  }
  const b = base.replace(/\/+$/, "");

  const options: { token: string; label: string }[] = [
    { token: "yes", label: "Yes😃" },
    { token: "meh", label: "Meh😐" },
    { token: "no", label: "No😔" },
  ];

  return (
    <Section className="ad-feedback-card" style={AD_FEEDBACK_INNER_MARGIN}>
      <Text
        style={{
          margin: "0 0 4px",
          fontSize: "14px",
          lineHeight: "21px",
          fontWeight: 600,
          color: "#5a1f1f",
          fontFamily: "Arial, Helvetica, sans-serif",
        }}
      >
        Was this ad helpful?
      </Text>
      {options.map((o) => (
        <Link
          key={o.token}
          href={`${b}/vote?choice=${encodeURIComponent(o.token)}`}
          style={{ ...adFeedbackOptStyle }}
        >
          {o.label}
        </Link>
      ))}
    </Section>
  );
}

function BulletLine(props: { index: number; segments: ItemSegment[] }) {
  const { index, segments } = props;

  const body = segments.map((s, si) =>
    s.type === "text" ? (
      <Fragment key={`t-${si}`}>{s.text}</Fragment>
    ) : (
      <Link
        key={`l-${si}`}
        href={s.href}
        style={{
          color: "#0b57d0",
          textDecoration: "underline",
        }}
      >
        {s.label}
      </Link>
    ),
  );

  return (
    <Text
      style={{
        fontSize: "14px",
        lineHeight: "21px",
        margin: "0 0 8px",
        color: "#2b303a",
        paddingLeft: "4px",
      }}
    >
      {index + 1}. {body}
    </Text>
  );
}

export function NewsFeedEmail({
  changeType,
  dateRaw,
  items,
  tldr,
  previewText,
  promoText,
  promoLogoUrl,
  promoLinkHref,
  adFeedbackBaseUrl = null,
}: NewsFeedEmailProps) {
  const header =
    changeType === "NEW_DATE"
      ? `New announcement posted for ${dateRaw}:`
      : `The announcement for ${dateRaw} was edited. Current content:`;

  return (
    <Html>
      <Head>
        <style>
          {`
            @media (prefers-color-scheme: dark) {
              .promo-callout {
                background-color: #1a1d24 !important;
                border-color: #3d4450 !important;
              }
              .promo-text {
                color: #e8eaed !important;
              }
              .ad-feedback-card {
                background-color: #4a2929 !important;
                border-color: #cf4a4a !important;
              }
            }
          `}
        </style>
      </Head>
      <Body
        style={{
          margin: 0,
          backgroundColor: "#ffffff",
          fontFamily: "Arial, Helvetica, sans-serif",
        }}
      >
        {previewText ? (
          <Section
            style={{
              display: "none",
              maxHeight: "0px",
              overflow: "hidden",
              lineHeight: "1px",
              opacity: 0,
            }}
          >
            <Text style={{ margin: 0, fontSize: "1px", color: "transparent" }}>
              {previewText}
            </Text>
          </Section>
        ) : null}
        <Container
          style={{ maxWidth: "600px", margin: "0 auto", padding: "24px 16px" }}
        >
          <EmailSummaryCard tldr={tldr} />
          <Text
            style={{
              fontSize: "15px",
              lineHeight: "22px",
              margin: "0 0 12px",
              color: "#1a1d24",
            }}
          >
            {header}
          </Text>
          {items.map((segments, i) => (
            <BulletLine key={i} index={i} segments={segments} />
          ))}
          {promoText ? (
            <PromoCallout
              promoText={promoText}
              promoLogoUrl={promoLogoUrl}
              promoLinkHref={promoLinkHref}
            />
          ) : null}
          <AdFeedbackMcq
            adFeedbackBaseUrl={
              promoText ? (adFeedbackBaseUrl ?? null) : null
            }
          />
          <Hr style={{ borderColor: "#e5e7eb", margin: "28px 0 16px" }} />
          <Text style={{ color: "#888888", fontSize: "12px", margin: 0 }}>
            Sent by Madhav&apos;s Canvas News Feed Monitor
          </Text>
        </Container>
      </Body>
    </Html>
  );
}
