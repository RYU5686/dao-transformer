import { daoTransform } from "./transformer.js";

const $=id=>document.getElementById(id);
const file=$("file"),drop=$("drop"),chosen=$("chosen"),name=$("name"),size=$("size");
const run=$("run"),remove=$("remove"),status=$("status"),state=$("state"),log=$("log"),download=$("download");

const fmt=n=>{let u=["B","KB","MB","GB"],i=0;while(n>=1024&&i<3){n/=1024;i++}return n.toFixed(i?1:0)+" "+u[i]};
function setFile(f){
 if(!f)return;
 if(!f.name.toLowerCase().endsWith(".mp4")) return alert("Please choose an MP4 file.");
 const dt=new DataTransfer();dt.items.add(f);file.files=dt.files;
 name.textContent=f.name;size.textContent=fmt(f.size);chosen.hidden=false;run.disabled=false;status.hidden=true;
}
file.onchange=()=>setFile(file.files[0]);
["dragenter","dragover"].forEach(e=>drop.addEventListener(e,x=>{x.preventDefault();drop.classList.add("drag")}));
["dragleave","drop"].forEach(e=>drop.addEventListener(e,x=>{x.preventDefault();drop.classList.remove("drag")}));
drop.addEventListener("drop",e=>setFile(e.dataTransfer.files[0]));
remove.onclick=()=>{file.value="";chosen.hidden=true;run.disabled=true;status.hidden=true};

run.onclick=async()=>{
 const f=file.files[0]; if(!f)return;
 run.disabled=true;status.hidden=false;download.hidden=true;state.textContent="Reading MP4…";log.textContent="";
 try{
   const bytes=new Uint8Array(await f.arrayBuffer());
   const out=daoTransform(bytes);
   const blob=new Blob([out],{type:"video/mp4"});
   download.href=URL.createObjectURL(blob);
   download.download="DAO_TRANSFORMED_"+f.name;
   download.hidden=false;
   state.textContent="DAO TRANSFORMATION COMPLETE";
   log.textContent="Output created locally.";
 }catch(e){
   state.textContent="Browser engine needs completion";
   log.textContent=e.message;
 }finally{run.disabled=false}
};
