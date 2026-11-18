import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { AttachmentTile, mediaTarget } from "@/components/AttachmentTile";
import { DeleteConfirm } from "@/components/DeleteConfirm";
import { faviconUrls, logoFallbackUrls, providerBrand } from "@/lib/provider-brand";

afterEach(cleanup);

describe("media privacy", () => {
  it("loads each remote image only after an explicit click", () => {
    const { container, rerender } = render(<AttachmentTile attachment={{ kind: "image", url: "https://audit.invalid/one.png" }} />);
    expect(container.querySelector("img")).toBeNull();
    fireEvent.click(screen.getByRole("button"));
    expect(container.querySelector("img")).toHaveAttribute("src", "https://audit.invalid/one.png");
    rerender(<AttachmentTile attachment={{ kind: "image", url: "https://audit.invalid/two.png" }} />);
    expect(container.querySelector("img")).toBeNull();
  });
  it("keeps signed local images available and rejects unsafe URLs", () => {
    const { container } = render(<AttachmentTile attachment={{ kind: "image", url: "/api/media/signed/image" }} />);
    expect(container.querySelector("img")).toHaveAttribute("src", "/api/media/signed/image");
    for (const url of ["javascript:alert(1)", "file:///etc/passwd", "data:text/html,bad", "https://user:password@example.com/a"]) {
      expect(mediaTarget(url)).toBeNull();
    }
  });
  it("blocks remote video preloading", () => {
    const { container } = render(<AttachmentTile attachment={{ kind: "video", url: "https://audit.invalid/movie.mp4" }} />);
    expect(container.querySelector("video")).toBeNull();
    fireEvent.click(screen.getByRole("button"));
    expect(container.querySelector("video")).toHaveAttribute("preload", "none");
  });
  it("never falls back to remote favicon services", () => {
    expect(faviconUrls("private-query.example")).toEqual([]);
    expect(providerBrand("openai")?.logoUrls).toEqual([]);
    expect(logoFallbackUrls("https://example.com/logo.svg")).toEqual([]);
    expect(logoFallbackUrls("//example.com/logo.svg")).toEqual([]);
    expect(logoFallbackUrls("/\\example.com/logo.svg")).toEqual([]);
    expect(logoFallbackUrls("/brand/logo.svg")).toEqual(["/brand/logo.svg"]);
  });
});

it("offers memory deletion by default with an explicit retention choice", () => {
  const results: boolean[] = [];
  render(<DeleteConfirm open title="Example" onCancel={() => {}} onConfirm={(value) => { results.push(value); }} />);
  const checkbox = screen.getByRole("checkbox");
  expect(checkbox).toBeChecked();
  fireEvent.click(checkbox);
  expect(checkbox).not.toBeChecked();
  fireEvent.click(screen.getByRole("button", { name: /Delete|삭제/ }));
  expect(results).toEqual([false]);
});
