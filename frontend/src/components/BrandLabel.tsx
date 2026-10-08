import cms from "../assets/brands/cms.png";
import green from "../assets/brands/green-foundation.png";
import vrutti from "../assets/brands/vrutti.png";
import swasti from "../assets/brands/swasti.png";
import upfront from "../assets/brands/upfront.png";
import setu from "../assets/brands/setu.png";
import communityActionCollab from "../assets/brands/community-action-collab.png";
import "./brand-label.css";
import { VERTICALS } from "@/lib/types";

const logos: Record<string, string> = {
  CMS: cms, "Green Foundation": green, Vrutti: vrutti, Swasti: swasti, Upfront: upfront,
  Setu: setu, "Community Action Collab": communityActionCollab,
};

export function BrandLabel({ name }: { name: string }) {
  return <span className="brand-label" title={name}>{logos[name] ? <span className="brand-logo-frame"><img src={logos[name]} alt={name} /></span> : <span>{name}</span>}</span>;
}

export function BrandBars({ values, verticals, onSelect, onVerticalSelect, onCmsSelect }: {
  values: Record<string, number>;
  verticals: Record<string, number>;
  onSelect: (name: string) => void;
  onVerticalSelect: (name: string) => void;
  onCmsSelect: () => void;
}) {
  const max = Math.max(1, ...Object.values(values));
  const heading = (name: string, value: number) => <>
      <span className="brand-chart-heading"><BrandLabel name={name}/><b>{value.toLocaleString()}</b></span>
      <span className="brand-chart-track"><span style={{width: `${value / max * 100}%`}}/></span>
    </>;
  const verticalMax = Math.max(1, ...Object.values(verticals));
  const verticalRow = (name: string) => <button type="button" key={name} onClick={() => onVerticalSelect(name)}>
    <span className="brand-chart-heading"><span>{name}</span><b>{(verticals[name] ?? 0).toLocaleString()}</b></span>
    <span className="brand-chart-track"><span style={{width: `${(verticals[name] ?? 0) / verticalMax * 100}%`}}/></span>
  </button>;
  return <div className="brand-chart">{Object.entries(values).map(([name, value]) => name === "CMS" ?
    <details className="brand-cms" key={name}>
      <summary aria-label="CMS classifications" title="Expand CMS classifications">{heading(name, value)}<span className="brand-expand-hint">Expand / collapse</span></summary>
      <div className="brand-children">
        <button type="button" className="brand-all" onClick={onCmsSelect}>View all CMS opportunities</button>
        <details open className="brand-devsol"><summary>Devsol</summary><div className="brand-children">{VERTICALS.filter(v => v !== "Social Business").map(verticalRow)}</div></details>
      </div>
    </details> :
    <button type="button" key={name} onClick={() => onSelect(name)} title={`${name}: ${value.toLocaleString()} opportunities`}>{heading(name, value)}</button>
  )}</div>;
}
