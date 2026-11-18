declare module '*.css';
declare module '*.css?text' {
  const stylesheet: string;
  export default stylesheet;
}
