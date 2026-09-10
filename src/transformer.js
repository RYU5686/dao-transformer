/*
 DAO Transformer — browser port core

 Source basis: patcher_reference.py.
 The Python reference performs byte-level MP4 box rewriting, sample-table trimming,
 track cloning, auxiliary-data appending, and offset rebuilding.

 This browser module currently implements the safe MP4 parser/validator foundation.
 It deliberately does NOT fake the Python transformation: unsupported structures
 are rejected instead of emitting a potentially corrupt MP4.

 Next port targets from the reference:
   - stsz/stsc/stts/co64/stco handling
   - ctts/stss/sdtp/saiz trimming
   - primary video/audio rebuild
   - cloned AAC track
   - auxiliary sample tail
   - moov/mdat offset convergence
*/

const text = (u8, a, b) => new TextDecoder("latin1").decode(u8.subarray(a,b));
const u32 = (v,p) => v.getUint32(p,false);
const u64 = (v,p) => Number(v.getBigUint64(p,false));

export function parseBoxes(bytes, start=0, end=bytes.length) {
  const out=[]; let pos=start;
  while(pos+8<=end){
    const view=new DataView(bytes.buffer,bytes.byteOffset,bytes.byteLength);
    const size32=u32(view,pos), type=text(bytes,pos+4,pos+8);
    let size=size32, header=8;
    if(size32===1){
      if(pos+16>end) throw new Error(`Invalid extended-size ${type} box.`);
      size=u64(view,pos+8); header=16;
    } else if(size32===0) size=end-pos;
    if(size<header || pos+size>end) throw new Error(`Invalid ${type} box at offset ${pos}.`);
    out.push({type,offset:pos,size,header,payloadStart:pos+header,payloadEnd:pos+size});
    pos += size;
  }
  if(pos!==end) throw new Error("MP4 box layout is not aligned.");
  return out;
}

export function inspectMp4(bytes){
  const top=parseBoxes(bytes);
  const types=top.map(x=>x.type);
  if(!types.includes("ftyp") || !types.includes("moov") || !types.includes("mdat"))
    throw new Error("Input must contain ftyp, moov and mdat.");
  if(types.includes("moof"))
    throw new Error("Fragmented MP4 requires the full browser fragment-flattening port; this build will not guess or corrupt the file.");
  return {top, size:bytes.length, fragmented:false};
}

/*
 The supplied Python reference itself documents the supported media/container target:
 non-fragmented MP4, H.264/H.265 video and AAC audio. Keep that contract visible in
 the browser UI until the complete byte-for-byte port is verified against test files.
*/
export function daoTransform(bytes){
  inspectMp4(bytes);
  throw new Error(
    "Browser DAO engine is not yet enabled for this file. " +
    "The supplied Python reference is server-side Python; a verified byte-level browser port " +
    "must be completed before producing output. No fake output is generated."
  );
}
