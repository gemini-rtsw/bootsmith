# RPM for Bootsmith.
#
# Ships the systemd unit, its host settings and the seed profiles -- NOT the
# application, which runs from the container image on GHCR. The unit is pinned
# to the image built from the same commit, so `rpm -q bootsmith` names exactly
# what runs and `dnf downgrade` is a real rollback.
#
# THE VERSION LIVES HERE. Bump specver; the image tag follows it.
%global specver 0.1.0

# $GIT_HASH first: build_rpm.sh computes it on the host and passes it into the
# build container. git alone can resolve to "nogit" there, and the unit would
# then pin an image tag that was never pushed.
%define git_hash %(if [ -n "$GIT_HASH" ]; then echo "$GIT_HASH"; else git rev-parse --short HEAD 2>/dev/null || echo nogit; fi)
%global appimage ghcr.io/gemini-rtsw/bootsmith

Name:           bootsmith
Version:        %{specver}
Release:        1.git%{git_hash}%{?dist}
Summary:        Bootsmith: VME crate boot console (runs as a container)

License:        Proprietary
URL:            https://github.com/gemini-rtsw/bootsmith
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  systemd-rpm-macros
Requires:       systemd
Requires(pre):  shadow-utils

%description
Web console for setting VME crate boot parameters over WTI terminal servers.

This package contains only the systemd unit, its settings and the seed
profiles. The application runs from the container image
%{appimage}:%{version}-git%{git_hash}, which the unit pulls on start.

    systemctl enable --now bootsmith
    curl -s localhost:8080/

%prep
%autosetup

%build
# Pin the NVR-unique tag, not the bare version: :<version> is retagged by every
# build of that version, which would make `dnf downgrade` a no-op for the image.
sed -e 's|@IMAGE@|%{appimage}:%{version}-git%{git_hash}|' \
    deploy/bootsmith.service.in > bootsmith.service
if grep -q '@IMAGE@' bootsmith.service; then
    echo "ERROR: image placeholder not substituted" >&2; exit 1
fi

%install
install -Dpm 0644 bootsmith.service        %{buildroot}%{_unitdir}/bootsmith.service
install -Dpm 0644 deploy/bootsmith.sysconfig %{buildroot}%{_sysconfdir}/sysconfig/bootsmith
install -dm 0755 %{buildroot}%{_sharedstatedir}/bootsmith/profiles
install -pm 0644 profiles/*.json            %{buildroot}%{_sharedstatedir}/bootsmith/profiles/

%pre
getent group bootsmith >/dev/null || groupadd -r bootsmith
getent passwd bootsmith >/dev/null || \
    useradd -r -g bootsmith -d %{_sharedstatedir}/bootsmith -s /sbin/nologin \
            -c "Bootsmith service" bootsmith
exit 0

%post
%systemd_post bootsmith.service

%preun
%systemd_preun bootsmith.service

%postun
# No restart on upgrade: someone may be mid-way through booting a crate. The
# new image takes effect on the next `systemctl restart bootsmith`.
%systemd_postun bootsmith.service

%files
# Not a config file: the unit carries the image tag, so upgrades must replace it.
%{_unitdir}/bootsmith.service
%config(noreplace) %{_sysconfdir}/sysconfig/bootsmith
%dir %attr(0755,bootsmith,bootsmith) %{_sharedstatedir}/bootsmith
%dir %attr(0755,bootsmith,bootsmith) %{_sharedstatedir}/bootsmith/profiles
# Seed profiles. The app edits them in place, so keep local edits on upgrade.
%config(noreplace) %attr(0644,bootsmith,bootsmith) %{_sharedstatedir}/bootsmith/profiles/*.json

%changelog
* Tue Oct 06 2026 Hawi Stecher <hawi.stecher@noirlab.edu> - 0.1.0-1
- Package for the gemini-rtsw-ci pipeline: systemd unit pinned to the image.
