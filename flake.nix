{
  description = "AI-Powered Payment Gateway Platform (case study) dev shell";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "aarch64-darwin" "x86_64-darwin" "x86_64-linux" "aarch64-linux" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = [
            # Python tooling runs in the dev container (docker/dev.Dockerfile):
            # host uv fails under paths containing a "private" component.
            pkgs.nodejs_22
            pkgs.postgresql_16
            pkgs.gnumake
          ];
        };
      });
    };
}
