# Graphiques et Analytics OpenSearch pour Payroll

Ce document décrit les graphiques disponibles dans OpenSearch pour le module payroll, basés sur les champs ajoutés dans `documents.py`.

## Différence entre Benefit et Benefit Plan

### Benefit (BenefitConsumption)
- **Définition** : Consommation réelle d'un bénéfice par un individu (instance concrète de paiement)
- **Table** : `payroll_benefitconsumption`
- **Liens** : 
  - `BenefitConsumption.individual` → `Individual` (l'individu qui reçoit)
  - `BenefitConsumption` → `PayrollBenefitConsumption` → `Payroll` (le paiement)
- **Données** : `amount`, `status`, `receipt`, `date_due`, `type`

### Benefit Plan (social_protection)
- **Définition** : Plan de bénéfice qui définit les règles et critères (configuration)
- **Table** : `social_protection_benefitplan`
- **Liens** :
  - `BenefitPlan` → `Beneficiary` → `Individual` (les bénéficiaires réels)
- **Données** : `code`, `name`, `max_beneficiaries`, `ceiling_per_beneficiary`, `type`

### Relation dans les statistiques
Les statistiques sont basées sur les **données réelles** :
- **Individual** (dans `individual_individual`) → **Beneficiary** (dans `social_protection_beneficiary`) → **BenefitPlan** (dans `social_protection_benefitplan`)
- **BenefitConsumption** est lié à un **Individual** qui est lié à un **Beneficiary** qui est lié à un **BenefitPlan**

**Important** : Les graphiques utilisent le `BenefitPlan` réel du `Beneficiary` de l'individu, pas le `BenefitPlan` du `PaymentPlan`.

## Champs ajoutés pour analytics

### BenefitConsumptionDocument
- `individual.gender` : Genre de l'individu (M/F/O)
- `benefit_plan.id` : ID du plan de bénéfice
- `benefit_plan.code` : Code du plan de bénéfice
- `benefit_plan.name` : Nom du plan de bénéfice
- `is_monetary_transfer` : Boolean indiquant si c'est un transfert monétaire
- `payment_cycle_code` : Code du cycle de paiement
- `payment_cycle_start_date` : Date de début du cycle
- `payment_cycle_end_date` : Date de fin du cycle

### PayrollBenefitConsumptionDocument
- `benefit.individual.gender` : Genre de l'individu
- `benefit.benefit_plan.*` : Informations du plan de bénéfice
- `benefit.is_monetary_transfer` : Boolean transfert monétaire
- `benefit.payment_cycle_code` : Code du cycle de paiement

### PayrollDocument
- `benefit_plan.*` : Informations du plan de bénéfice

---

## Graphiques disponibles

### 1. Nombre d'individus par Benefit Plan

**Type** : Bar chart / Pie chart
**Source (Data View)** : `benefit_consumption*`

**Requête OpenSearch** :
```json
{
  "size": 0,
  "aggs": {
    "benefit_plans": {
      "terms": {
        "field": "benefit_plan.code.keyword",
        "size": 100
      },
      "aggs": {
        "unique_individuals": {
          "cardinality": {
            "field": "individual.id.keyword"
          }
        }
      }
    }
  }
}
```

**Index** : `benefit_consumption`

**Visualisation** : Bar chart avec `benefit_plan.code` en X et `unique_individuals.value` en Y

---

### 2. Pourcentage Homme/Femme par Benefit Plan

**Type** : Stacked bar chart / Pie chart
**Source (Data View)** : `benefit_consumption*`

**Requête OpenSearch** :
```json
{
  "size": 0,
  "aggs": {
    "by_benefit_plan": {
      "terms": {
        "field": "benefit_plan.code.keyword",
        "size": 100
      },
      "aggs": {
        "by_gender": {
          "terms": {
            "field": "individual.gender.keyword",
            "size": 3
          },
          "aggs": {
            "unique_individuals": {
              "cardinality": {
                "field": "individual.id.keyword"
              }
            }
          }
        },
        "total_individuals": {
          "cardinality": {
            "field": "individual.id.keyword"
          }
        }
      }
    }
  }
}
```

**Index** : `benefit_consumption`

**Visualisation** : Stacked bar chart avec pourcentages calculés

---

### 3. Nombre d'individus ayant bénéficié de transfert monétaire par Benefit Plan

**Type** : Bar chart
**Source (Data View)** : `benefit_consumption*`

**Requête OpenSearch** :
```json
{
  "size": 0,
  "query": {
    "term": {
      "is_monetary_transfer": true
    }
  },
  "aggs": {
    "by_benefit_plan": {
      "terms": {
        "field": "benefit_plan.code.keyword",
        "size": 100
      },
      "aggs": {
        "unique_individuals": {
          "cardinality": {
            "field": "individual.id.keyword"
          }
        }
      }
    }
  }
}
```

**Index** : `benefit_consumption`

---

### 4. Pourcentage Homme/Femme ayant bénéficié de transfert monétaire par Benefit Plan et Cycle de Paiement

**Type** : Heatmap / Multi-series bar chart
**Source (Data View)** : `benefit_consumption*`

**Requête OpenSearch** :
```json
{
  "size": 0,
  "query": {
    "term": {
      "is_monetary_transfer": true
    }
  },
  "aggs": {
    "by_benefit_plan": {
      "terms": {
        "field": "benefit_plan.code.keyword",
        "size": 50
      },
      "aggs": {
        "by_payment_cycle": {
          "terms": {
            "field": "payment_cycle_code.keyword",
            "size": 20
          },
          "aggs": {
            "by_gender": {
              "terms": {
                "field": "individual.gender.keyword",
                "size": 3
              },
              "aggs": {
                "unique_individuals": {
                  "cardinality": {
                    "field": "individual.id.keyword"
                  }
                }
              }
            },
            "total_individuals": {
              "cardinality": {
                "field": "individual.id.keyword"
              }
            }
          }
        }
      }
    }
  }
}
```

**Index** : `benefit_consumption`

**Visualisation** : Heatmap avec axes Benefit Plan × Payment Cycle, couleur = pourcentage par genre

---

### 5. Nombre d'individus par Paiement (croissance/décroissance)

**Type** : Line chart / Area chart
**Source (Data View)** : `benefit_consumption*` ou `payroll_benefit_consumption*`

**Requête OpenSearch** :
```json
{
  "size": 0,
  "aggs": {
    "by_payroll_date": {
      "date_histogram": {
        "field": "date_created",
        "calendar_interval": "month",
        "format": "yyyy-MM"
      },
      "aggs": {
        "unique_individuals": {
          "cardinality": {
            "field": "individual.id.keyword"
          }
        }
      }
    }
  }
}
```

**Index** : `benefit_consumption` ou `payroll_benefit_consumption`

**Visualisation** : Line chart avec tendance (croissance/décroissance/stabilité)

**Variantes** :
- Par Benefit Plan : ajouter un sous-aggrégation `by_benefit_plan`
- Par cycle de paiement : utiliser `payment_cycle_start_date` au lieu de `date_created`

---

## Graphiques supplémentaires proposés

### 6. Montant total des transferts monétaires par Benefit Plan

**Type** : Bar chart / Pie chart
**Source (Data View)** : `benefit_consumption*`

**Requête OpenSearch** :
```json
{
  "size": 0,
  "query": {
    "term": {
      "is_monetary_transfer": true
    }
  },
  "aggs": {
    "by_benefit_plan": {
      "terms": {
        "field": "benefit_plan.code.keyword",
        "size": 100
      },
      "aggs": {
        "total_amount": {
          "sum": {
            "field": "amount"
          }
        },
        "avg_amount": {
          "avg": {
            "field": "amount"
          }
        }
      }
    }
  }
}
```

**Index** : `benefit_consumption`

---

### 7. Taux de réconciliation par Benefit Plan

**Type** : Gauge / Bar chart
**Source (Data View)** : `benefit_consumption*`

**Requête OpenSearch** :
```json
{
  "size": 0,
  "aggs": {
    "by_benefit_plan": {
      "terms": {
        "field": "benefit_plan.code.keyword",
        "size": 100
      },
      "aggs": {
        "reconciled": {
          "filter": {
            "term": {
              "status": "RECONCILED"
            }
          }
        },
        "total": {
          "value_count": {
            "field": "id"
          }
        }
      }
    }
  }
}
```

**Index** : `benefit_consumption`

**Visualisation** : Gauge avec pourcentage (reconciled / total)

---

### 8. Distribution des montants par Benefit Plan

**Type** : Histogram / Box plot
**Source (Data View)** : `benefit_consumption*`

**Requête OpenSearch** :
```json
{
  "size": 0,
  "query": {
    "term": {
      "is_monetary_transfer": true
    }
  },
  "aggs": {
    "by_benefit_plan": {
      "terms": {
        "field": "benefit_plan.code.keyword",
        "size": 50
      },
      "aggs": {
        "amount_distribution": {
          "histogram": {
            "field": "amount",
            "interval": 1000
          }
        },
        "stats": {
          "stats": {
            "field": "amount"
          }
        }
      }
    }
  }
}
```

**Index** : `benefit_consumption`

---

### 9. Évolution du nombre de bénéficiaires par cycle de paiement

**Type** : Line chart
**Source (Data View)** : `benefit_consumption*`

**Requête OpenSearch** :
```json
{
  "size": 0,
  "aggs": {
    "by_payment_cycle": {
      "terms": {
        "field": "payment_cycle_code.keyword",
        "order": {
          "_key": "asc"
        },
        "size": 100
      },
      "aggs": {
        "by_date": {
          "date_histogram": {
            "field": "payment_cycle_start_date",
            "calendar_interval": "month"
          },
          "aggs": {
            "unique_individuals": {
              "cardinality": {
                "field": "individual.id.keyword"
              }
            }
          }
        }
      }
    }
  }
}
```

**Index** : `benefit_consumption`

---

### 10. Taux de participation par genre et par région (si location disponible)

**Type** : Stacked bar chart
**Source (Data View)** : `benefit_consumption*`

**Requête OpenSearch** :
```json
{
  "size": 0,
  "aggs": {
    "by_benefit_plan": {
      "terms": {
        "field": "benefit_plan.code.keyword",
        "size": 50
      },
      "aggs": {
        "by_gender": {
          "terms": {
            "field": "individual.gender.keyword",
            "size": 3
          },
          "aggs": {
            "unique_individuals": {
              "cardinality": {
                "field": "individual.id.keyword"
              }
            }
          }
        }
      }
    }
  }
}
```

**Index** : `benefit_consumption`

---

### 11. Tendance des paiements (statut) au fil du temps

**Type** : Stacked area chart
**Source (Data View)** : `benefit_consumption*`

**Requête OpenSearch** :
```json
{
  "size": 0,
  "aggs": {
    "by_date": {
      "date_histogram": {
        "field": "date_created",
        "calendar_interval": "month"
      },
      "aggs": {
        "by_status": {
          "terms": {
            "field": "status.keyword",
            "size": 10
          }
        }
      }
    }
  }
}
```

**Index** : `benefit_consumption`

---

### 12. Top 10 des bénéficiaires par montant reçu

**Type** : Bar chart horizontal
**Source (Data View)** : `benefit_consumption*`

**Requête OpenSearch** :
```json
{
  "size": 0,
  "query": {
    "term": {
      "is_monetary_transfer": true
    }
  },
  "aggs": {
    "by_individual": {
      "terms": {
        "field": "individual.id.keyword",
        "size": 10,
        "order": {
          "total_amount": "desc"
        }
      },
      "aggs": {
        "total_amount": {
          "sum": {
            "field": "amount"
          }
        },
        "individual_name": {
          "top_hits": {
            "size": 1,
            "_source": {
              "includes": ["individual.first_name", "individual.last_name"]
            }
          }
        }
      }
    }
  }
}
```

**Index** : `benefit_consumption`

---

## Notes d'implémentation

1. **Réindexation nécessaire** : Après modification de `documents.py`, il faut réindexer les données :
   ```bash
   python manage.py opensearch_index --rebuild
   ```

2. **Performance** : Les agrégations avec `cardinality` peuvent être lentes sur de gros volumes. Utiliser `precision_threshold` si nécessaire.

3. **Filtres temporels** : Ajouter des filtres de date dans les requêtes pour limiter la période analysée :
   ```json
   "query": {
     "bool": {
       "must": [
         {"range": {"date_created": {"gte": "2024-01-01", "lte": "2024-12-31"}}}
       ]
     }
   }
   ```

4. **Dashboards OpenSearch** : Créer des dashboards dans OpenSearch Dashboards en utilisant ces requêtes comme base pour les visualisations.

