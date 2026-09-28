import cms from "../assets/brands/cms.png";
import green from "../assets/brands/green-foundation.png";
import vrutti from "../assets/brands/vrutti.png";
import swasti from "../assets/brands/swasti.png";
import upfront from "../assets/brands/upfront.png";
import "./brand-label.css";

const logos: Record<string, string> = {
  CMS: cms, "Green Foundation": green, Vrutti: vrutti, Swasti: swasti, Upfront: upfront,
};

export function BrandLabel({ name }: { name: string }) {
  return <span className="brand-label">{logos[name] && <span className="brand-logo-frame"><img src={logos[name]} alt="" /></span>}<span>{name}</span></span>;
}

export function BrandBars({ values, onSelect }: { values: Record<string, number>; onSelect: (name: string) => void }) {
  const max = Math.max(1, ...Object.values(values));
  return <div className="brand-chart">{Object.entries(values).map(([name, value]) =>
    <button type="button" key={name} onClick={() => onSelect(name)} title={`${name}: ${value.toLocaleString()} opportunities`}>
      <span className="brand-chart-heading"><BrandLabel name={name}/><b>{value.toLocaleString()}</b></span>
      <span className="brand-chart-track"><span style={{width: `${value / max * 100}%`}}/></span>
    </button>
  )}</div>;
}
