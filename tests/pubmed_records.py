"""Builders for fake PubMed records with the structure returned by Bio.Entrez.read (efetch, db=pubmed)."""


class Tagged(str):
    """A string with XML attributes, like Bio.Entrez's StringElement."""

    def __new__(cls, value, **attributes):
        obj = super().__new__(cls, value)
        obj.attributes = attributes
        return obj


def make_article(pmid, title="A cohort study", year="2020", pub_types=("Journal Article",),
                 mesh=(("Child", "D002648"),), abstract="Background. Methods. Results.",
                 doi="10.1000/test.1", doi_in="elocation",
                 authors=(("Smith", "J", ["Dept X, Univ A, City", "Hosp B"]), ("Lee", "K", ["Univ C"])),
                 journal="Test journal", nlm_id="0000001", medline_ta="Test J"):
    """One PubmedArticle record. doi_in: 'elocation' (Article/ELocationID), 'articleid' (PubmedData) or None."""
    article = {
        "ArticleTitle": title,
        "Journal": {"Title": journal, "ISSN": "1234-5678",
                    "JournalIssue": {"Volume": "1", "Issue": "2", "PubDate": {"Year": year}}},
        "PublicationTypeList": [Tagged(t, UI="D016428") for t in pub_types],
        "AuthorList": [{"LastName": last, "Initials": initials,
                        "AffiliationInfo": [{"Affiliation": a} for a in affiliations]}
                       for last, initials, affiliations in authors],
        "Pagination": {"MedlinePgn": "1-10"},
    }
    if abstract:
        article["Abstract"] = {"AbstractText": [abstract]}
    if doi and doi_in == "elocation":
        article["ELocationID"] = [Tagged(doi, EIdType="doi", ValidYN="Y")]
    citation = {
        "PMID": Tagged(str(pmid), Version="1"),
        "Article": article,
        "MedlineJournalInfo": {"NlmUniqueID": nlm_id, "MedlineTA": medline_ta},
    }
    if mesh:
        citation["MeshHeadingList"] = [{"DescriptorName": Tagged(term, UI=uid, MajorTopicYN="N")} for term, uid in mesh]
    pubmed_data = {"ArticleIdList": [Tagged(str(pmid), IdType="pubmed")]}
    if doi and doi_in == "articleid":
        pubmed_data["ArticleIdList"].append(Tagged(doi, IdType="doi"))
    return {"MedlineCitation": citation, "PubmedData": pubmed_data}


def efetch_result(*articles):
    """The dict Entrez.read returns for an efetch call."""
    return {"PubmedArticle": list(articles), "PubmedBookArticle": []}


def mesh_string(*pairs):
    """('Term', 'D000001'), ... -> "('Term','D000001'),..." as stored in the mesh_terms column."""
    return ",".join(f"('{term}','{uid}')" for term, uid in pairs)
